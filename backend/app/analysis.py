"""Stateless A -> B -> contextual mapping -> C/E -> D orchestration."""
import json
from decimal import Decimal, InvalidOperation

from app.catalog import Snapshot
from app.units import get_unit
from app.components import Components, ExistingSearcher, SearchInput, existing_components
from app.extraction import MEASUREMENT, apply_inputs, normalize
from app.questions import property_question, property_question_id, ranges_hint
from app.correction_flow import resolve_suggestions
from app.catalog_diagnostics import position_blockers
from app.schemas import (AnalysisCreate, AnalysisOut, Correction, Fact, Fragment, Issue, Question, Scope,
                         Verification)

#: Коды, означающие «значение ответа недопустимо». Ответ с таким кодом не считается
#: отвеченным: вопрос повторяется, чтобы пользователь ввёл корректное значение.
INVALID_ANSWER_CODES = frozenset({"invalid_value", "invalid_number", "invalid_type", "invalid_count",
                                  "out_of_range", "unit_mismatch", "unit_missing", "unit_unknown",
                                  "numeric_semantics_unknown", "multiple_values_not_allowed"})

#: Во сколько раз значение может превышать наблюдаемый в КТРУ диапазон характеристики.
#: Справочник иногда оставляет верхнюю границу открытой («900 и более мм» у длины рукоятки
#: молотка), и тогда формально допустимо любое значение — «200 метров» проходили и доходили
#: до подтверждения кода. Границы наблюдаемых значений берутся из самого каталога, поэтому
#: это не выдуманный предел, а проверка масштаба: двукратный выход — предупреждение,
#: десятикратный — ошибка, которая не даёт подтвердить код.
SCALE_WARNING_FACTOR = 2
SCALE_ERROR_FACTOR = 10

#: Кэш наблюдаемых границ по версии каталога: обход 14 тысяч позиций делается один раз.
_ENVELOPE_CACHE: dict[str, dict[tuple[str, str, str], tuple[Decimal, Decimal, str, Decimal]]] = {}


def group_prefix(code: str) -> str:
    """Группа КТРУ позиции: код без номера позиции («06.20.10.110-00000003» → «06.20.10.110»)."""
    return code.split("-")[0]


def catalogue_envelope(data: Snapshot) -> dict[tuple[str, str, str], tuple[Decimal, Decimal, str, Decimal]]:
    """Наблюдаемые границы числовых характеристик — по группе КТРУ, а не по всему каталогу.

    Ключ — (название характеристики, размерность, группа КТРУ), значение — (минимум, максимум,
    размерность) В ОСНОВНЫХ единицах размерности. Две поправки, обе по делу:

    * группировка по группе КТРУ. Иначе одна и та же характеристика смешивала разные товары:
      «Объем баллона» у аэрозолей измеряется миллилитрами (0.5–80 мл), а у газовых баллонов —
      литрами, и нормальные 11 л выглядели выходом за масштаб в 137 раз.
    * пересчёт в основные единицы. Без него сравнивались числа в миллилитрах и литрах.

    Учитываются только интервалы с ОБЕИМИ границами: открытые концы о масштабе не говорят.
    Результат кэшируется по версии каталога.
    """
    version = getattr(getattr(data, "catalog", None), "catalog_version", "") or ""
    cached = _ENVELOPE_CACHE.get(version)
    if cached is not None:
        return cached
    envelope: dict[tuple[str, str, str], tuple[Decimal, Decimal, str, Decimal]] = {}
    for position in data.positions.values():
        prefix = group_prefix(position.code)
        for prop in position.properties:
            for item in prop.allowed:
                if item.kind not in ("interval", "number") or item.lower in (None, "") or item.upper in (None, ""):
                    continue
                unit = get_unit(item.unit) or (data.resolve_unit(prop, item.unit) if item.unit else None)
                if unit is None:
                    continue
                try:
                    lower, upper = Decimal(str(item.lower)), Decimal(str(item.upper))
                except (InvalidOperation, ValueError):
                    continue
                key = (canon_name(prop.name), unit.dimension, prefix)
                lower, upper = lower * unit.factor, upper * unit.factor
                current = envelope.get(key)
                if current is None:
                    envelope[key] = (lower, upper, unit.name, unit.factor)
                else:
                    envelope[key] = (min(current[0], lower), max(current[1], upper), current[2], current[3])
    _ENVELOPE_CACHE[version] = envelope
    return envelope


def canon_name(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split())


def property_unit(prop) -> str | None:
    """Единица характеристики — единственная у неё или у вопроса.

    Пользователь не выбирает единицы: он пишет значение в той единице, что есть в карточке.
    Если единица одна, ответ без единицы получает её автоматически, и «число без единицы» не
    превращается в ошибку unit_missing.
    """
    units = {item.unit for item in prop.allowed if item.unit}
    return next(iter(units)) if len(units) == 1 else None


def include_same_named(data: Snapshot, category_id: str | None, positions, scope_complete: bool):
    """Одноимённые семейства КТРУ — один товар: собираем их позиции в общий пул.

    В каталоге 893 имени имеют дубли, у 135 характеристики различаются («Протез пениса
    жёсткий» — 16 семейств по 1 позиции). Поиск уже не повторяет одноимённые варианты, но
    выбор одного семейства давал пул только его позиций, и остальные коды терялись. Здесь пул
    расширяется до всех одноимённых семейств, а сам каталог не переписывается.
    """
    from app.search import canon
    category = (data.categories or {}).get(category_id) if category_id else None
    if category is None:
        return positions, scope_complete
    key = canon(category.name)
    known = {position.code for position in positions}
    extra: list = []
    complete = scope_complete
    for other_id, other in (data.categories or {}).items():
        if other_id == category_id or canon(other.name) != key:
            continue
        # Полноту области определяет выбранное семейство: добор одноимённых позиций не должен
        # блокировать подтверждение кода (иначе 15 позиций «Протеза пениса жёсткого» нельзя
        # довести до конца из-за неподтверждённого покрытия одного из 16 семейств).
        part, _part_complete = data.pool(other_id)
        extra.extend(position for position in part if position.code not in known)
        known.update(position.code for position in part)
    return tuple(positions) + tuple(extra), complete


def observed_range(data: Snapshot, name: str, groups: set[str]):
    """Наблюдаемый диапазон характеристики у найденных групп — для подсказки в вопросе.

    Нужен там, где справочник границ не даёт («900 и более мм»): иначе подсказка выглядит
    как «Допустимо: мм». Показывается в единице карточки (мм), а не в единице ввода (м).
    """
    from app.schemas import QuestionRange
    envelope = catalogue_envelope(data)
    key_name = canon_name(name)
    for key, value in envelope.items():
        if key[0] != key_name or key[2] not in groups:
            continue
        lower, upper, unit_name, factor = value
        return QuestionRange(lower=float(lower / factor), upper=float(upper / factor), unit=unit_name)
    return None


def clear_unknown_properties(result, positions, answers, registry) -> None:
    """Ответ «Не знаю» снимает прежнее значение характеристики.

    Без этого оставались две строки по одной характеристике (текстовое значение и «Не знаю»),
    а блокирующее замечание по снятому значению возвращало тот же вопрос — экран зацикливался
    на «Не знаю». Проход идёт ПОСЛЕ resolve_suggestions: только там текстовый факт получает
    имя характеристики, и его можно сопоставить.
    """
    unknown = [registry[answer.question_id] for answer in answers
               if answer.unknown and answer.question_id in registry]
    if not unknown:
        return
    keys = {(canon_name(prop.name), prop.subject) for prop in unknown}
    keys |= {(canon_name(alias), prop.subject) for prop in unknown for alias in prop.aliases}
    stale = [fact for fact in result.facts
             if fact.source != "answer" and (canon_name(fact.name), fact.subject) in keys]
    if not stale:
        return
    result.facts = [fact for fact in result.facts if fact not in stale]
    for fact in stale:
        result.corrections.append(Correction(fact_id=fact.id, kind="proposed_fact_change",
            original=fact.raw, normalized="не знаю",
            explanation="Пользователь ответил «Не знаю»; прежнее значение характеристики снято, подбор идёт без него"))

def auto_single_values(result, positions, facts: list) -> None:
    """Единственное допустимое значение обязательной характеристики подставляется само.

    Если у характеристики во ВСЕХ найденных позициях ровно одно возможное значение
    («Диаметр: 13 мм»), спрашивать нечего: вопрос лишь заставляет нажать «Продолжить».
    Подставляются точные числа и вырожденные интервалы (13–13): перечисления и настоящие
    диапазоны оставляем пользователю, там выбор содержательный.
    """
    groups: dict[tuple, list] = {}
    for position in positions:
        for prop in position.properties:
            groups.setdefault((prop.semantic_key or prop.id, prop.subject), []).append(prop)
    known = {(canon_name(fact.name), fact.subject) for fact in facts if not fact.unknown}
    for (_, subject), props in groups.items():
        first = props[0]
        if first.required is not True or (canon_name(first.name), subject) in known:
            continue
        if len({item.model_dump_json() for prop in props for item in prop.allowed}) != 1:
            continue
        item = first.allowed[0]
        if item.kind == "number" and item.value not in (None, ""):
            value, unit = str(item.value), item.unit
        elif item.kind == "interval" and item.lower not in (None, "") and item.lower == item.upper:
            value, unit = str(item.lower), item.unit
        else:
            continue
        facts.append(Fact(id=f"single:{first.id}", name=first.name, value=value, unit=unit,
                          raw=f"{value} {unit or ''}".strip(), source="input", subject=subject,
                          unknown=False))
        result.corrections.append(Correction(fact_id=f"single:{first.id}", kind="auto_normalization",
            original="", normalized=f"{value} {unit or ''}".strip(),
            explanation="Единственное допустимое значение характеристики подставлено автоматически"))

def out_of_scale_values(data: Snapshot, facts: list, rejected, groups: set[str] | None = None) -> list[Issue]:
    """Значения, выходящие за наблюдаемый в КТРУ масштаб характеристики.

    «200 метров» у длины рукоятки молотка: справочник допускает «900 и более мм», поэтому
    обычная проверка границ молчала, и значение доходило до подтверждения кода. Здесь
    сравниваем с тем, что реально встречается в КТРУ **у найденных групп** — двукратный выход
    даёт предупреждение, десятикратный ошибку, которая не даёт подтвердить код.
    """
    envelope = catalogue_envelope(data)
    if not envelope:
        return []
    issues: list[Issue] = []
    for fact in facts:
        if fact.id in rejected or not fact.name or not fact.unit or fact.value in (None, ""):
            continue
        unit = get_unit(fact.unit)
        if unit is None:
            continue
        candidates = [key for key in ((canon_name(fact.name), unit.dimension, prefix)
                                      for prefix in (groups or set()))
                      if key in envelope]
        if not candidates:
            continue
        lower = min(envelope[key][0] for key in candidates)
        upper = max(envelope[key][1] for key in candidates)
        try:
            value = Decimal(str(fact.value)) * unit.factor
        except (InvalidOperation, ValueError):
            continue
        factor = SCALE_ERROR_FACTOR if upper > 0 and value > upper * SCALE_ERROR_FACTOR else SCALE_WARNING_FACTOR
        if value <= upper * factor and (lower <= 0 or value >= lower / factor):
            continue
        # Границы — в единице карточки (мм), а не в единице ввода (м): пользователь пишет
        # значение, но сверяется с тем, что записано в справочнике.
        catalogue_unit = envelope[candidates[0]][2]
        catalogue_factor = envelope[candidates[0]][3] or Decimal(1)
        displayed_lower = lower / catalogue_factor
        displayed_upper = upper / catalogue_factor
        shown = fact.raw or f"{fact.value} {fact.unit or ''}".strip()
        issues.append(Issue(
            code="value_out_of_scale", severity="error" if factor == SCALE_ERROR_FACTOR else "warning",
            fact_id=fact.id,
            explanation=f"{fact.name}: «{shown}» — у найденных позиций эта характеристика встречается "
                        f"от {_short(displayed_lower)} до {_short(displayed_upper)} {catalogue_unit}".strip(),
            suggested_action="Проверьте значение: похоже, указана не та единица или не то число"))
    return issues


def merge_answer_issues(issues: list[Issue]) -> list[Issue]:
    """Один неверный ответ — одно замечание вместо строки на каждую позицию.

    Каждая позиция перечисляет свои допустимые значения, поэтому про один и тот же ответ
    приходило до двенадцати почти одинаковых сообщений, различавшихся только хвостом
    «Допустимо: …». Склеиваем их: общий текст один, допустимые значения — объединение.
    Замечания не от ответа не трогаем: там позиция важна.
    """
    merged: dict[tuple, Issue] = {}
    order: list[tuple] = []
    for issue in issues:
        if not (issue.fact_id and issue.fact_id.startswith("answer:")):
            key = ("__raw__", id(issue))
            merged[key] = issue
            order.append(key)
            continue
        key = (issue.code, issue.fact_id, issue.property_id)
        if key not in merged:
            merged[key] = issue.model_copy()
            order.append(key)
            continue
        current = merged[key]
        head, _, tail = current.explanation.partition(". Допустимо:")
        allowed = [item.strip(" .") for item in tail.split(",") if item.strip(" .")] if tail else []
        extra_head, _, extra_tail = issue.explanation.partition(". Допустимо:")
        if extra_tail:
            allowed.extend(item.strip(" .") for item in extra_tail.split(",") if item.strip(" ."))
        current.explanation = head + (". Допустимо: " + ", ".join(dict.fromkeys(allowed)) + "." if allowed else "")
    return [merged[key] for key in order]


def reask_after_invalid(category_id: str, positions, facts, issues) -> tuple[Issue, "Question"] | None:
    """Вопрос про значение, которое нужно исправить, — вместо перехода дальше.

    Два случая, и оба означают «пользователь остаётся на этом шаге»:

    * ответ был недопустимым (код пришёл от ответа) — вопрос задаётся снова;
    * в тексте найдено неправдоподобное значение (`value_out_of_scale`, `implausible_value`,
      выход за границы) — спрашиваем именно эту характеристику, с диапазоном из карточки,
      иначе поток уходил к следующему вопросу или к выбору кода, а вброс оставался в карточке.
    """
    blocking = INVALID_ANSWER_CODES | {"value_out_of_scale", "implausible_value"}
    head = next((issue for issue in issues
                 if issue.code in blocking and issue.severity == "error"), None)
    if head is None:
        return None
    registry = {property_question_id(category_id, prop): prop
                for position in positions for prop in position.properties}
    prop = None
    if head.fact_id and head.fact_id.startswith("answer:"):
        prop = registry.get(head.fact_id.removeprefix("answer:"))
    if prop is None and head.property_id:
        prop = next((item for item in registry.values() if item.id == head.property_id), None)
    if prop is None:
        fact = next((item for item in facts if item.id == head.fact_id), None)
        if fact is not None and fact.name:
            key = canon_name(fact.name)
            prop = next((item for item in registry.values()
                         if canon_name(item.name) == key and item.subject == fact.subject), None)
    if prop is None:
        return None
    key = (prop.semantic_key or prop.id, prop.subject)
    variants = {item.model_dump_json(): item for position in positions for other in position.properties
                if (other.semantic_key or other.id, other.subject) == key and other.verified
                for item in other.allowed}
    merged = prop.model_copy(update={"allowed": tuple(variants.values())})
    reason = ("Значение недопустимо — укажите корректное" if head.code in ("invalid_value", "invalid_number",
                                                                           "invalid_count", "out_of_range")
              else "Значение неправдоподобно — проверьте единицу и число")
    return head, property_question(category_id, merged, reason)


def implausible_measurements(data: Snapshot, positions, remaining, facts, rejected) -> list[Issue]:
    """Измерения вне диапазонов ВСЕХ найденных позиций — их нельзя принимать молча.

    Зачем: «молоток 200 метров» — вброс, но до этой проверки он либо не разбирался как
    измерение, либо уходил в карточку без замечаний, потому что не был привязан к
    характеристике. Границы берутся только из справочника найденных позиций (по размерности
    и с пересчётом единиц), никаких собственных пределов в коде нет: если значение не
    попадает ни в один интервал ни одной характеристики оставшихся позиций — показываем
    пользователю допустимый диапазон и просим исправить значение.
    """
    pool = {position.code: position for position in positions}
    codes = {candidate.ktru_code for candidate in remaining}
    by_dimension: dict[str, list[tuple[Decimal, Decimal, str]]] = {}
    for code in codes:
        position = pool.get(code)
        if position is None:
            continue
        for prop in position.properties:
            for item in prop.allowed:
                if item.kind not in ("interval", "number"):
                    continue
                unit = get_unit(item.unit) or (data.resolve_unit(prop, item.unit) if item.unit else None)
                if unit is None:
                    continue
                lower = Decimal(str(item.lower)) if item.lower not in (None, "") else None
                upper = Decimal(str(item.upper)) if item.upper not in (None, "") else None
                if item.kind == "number" and item.value not in (None, ""):
                    lower = upper = Decimal(str(item.value))
                if lower is None and upper is None:
                    continue
                by_dimension.setdefault(unit.dimension, []).append(
                    (lower * unit.factor if lower is not None else Decimal("-Infinity"),
                     upper * unit.factor if upper is not None else Decimal("Infinity"),
                     unit.name))
    if not by_dimension:
        return []
    issues: list[Issue] = []
    for fact in facts:
        # Значение проверяем даже у факта с неустановленным свойством: это по-прежнему
        # утверждение о товаре («200 метров» у молотка), и молча его принимать нельзя.
        if fact.id in rejected or not fact.unit or fact.value in (None, ""):
            continue
        unit = get_unit(fact.unit)
        if unit is None or unit.dimension not in by_dimension:
            continue
        try:
            value = Decimal(str(fact.value)) * unit.factor
        except (InvalidOperation, ValueError):
            continue
        intervals = by_dimension[unit.dimension]
        if any(lower <= value <= upper for lower, upper, _ in intervals):
            continue
        # Допустимое показываем в единице пользователя: так значение понятнее.
        parts = []
        for lower, upper, _ in intervals:
            if lower.is_finite() and upper.is_finite():
                parts.append(f"от {_short(lower / unit.factor)} до {_short(upper / unit.factor)}")
            elif upper.is_finite():
                parts.append(f"до {_short(upper / unit.factor)}")
            elif lower.is_finite():
                parts.append(f"от {_short(lower / unit.factor)}")
        bounds = " или ".join(dict.fromkeys(parts)) or "значение не ограничено справочником"
        displayed = fact.raw or f"{fact.value} {fact.unit or ''}".strip()
        issues.append(Issue(code="implausible_value", severity="error", fact_id=fact.id,
                            explanation=f"Значение «{displayed}» не встречается ни у одной характеристики "
                                        f"найденных позиций: допустимо {bounds} {unit.name}".strip(),
                            suggested_action="Укажите правдоподобное значение или другую характеристику"))
    return issues


def _short(value: Decimal) -> str:
    text = f"{value.normalize():f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


class InvalidConfirmation(ValueError):
    def __init__(self, field: str, code: str):
        self.field, self.code = field, code


def analyze(data: Snapshot, search, body: AnalysisCreate, components: Components | None = None) -> AnalysisOut:
    components = components or existing_components()
    category_id = body.confirmed_category_id
    if category_id and category_id not in data.categories:
        raise InvalidConfirmation("confirmed_category_id", "unknown_category")
    positions, scope_complete = data.pool(category_id) if category_id else ((), False)
    positions, scope_complete = include_same_named(data, category_id, positions, scope_complete)
    properties = tuple(prop for position in positions for prop in position.properties)
    context = getattr(components.extractor, 'context', lambda scope, props: (props, (), ()))
    extraction_properties, card_properties, card_evidence = context(category_id, properties)
    # Before confirmation, a global adjective is not a property label. Explicit
    # keys still survive; only unambiguous physical labels use the global dictionary.
    names = tuple(sorted({name for prop in extraction_properties for name in (prop.name, *prop.aliases)})) if category_id else tuple(
        name for name in data.extraction_names if len(name.split()) > 1 or name in {'Толщина', 'Ширина', 'Высота', 'Длина', 'Диаметр'})
    contextual_extract = getattr(components.extractor, 'extract_context', None)
    extraction = (contextual_extract(body.description, names, scoped_properties=extraction_properties, category_id=category_id)
                  if contextual_extract else components.extractor.extract(body.description, names, scoped_properties=properties)) if components.extractor else None
    extracted, unparsed = (extraction.facts, extraction.unparsed) if extraction else ([], [Fragment(raw=body.description)])
    result = AnalysisOut(status="needs_data", original_text=body.description, normalized_text=normalize(body.description),
                         facts=extracted, unparsed=unparsed, additional=[Fragment(raw=raw) for raw in body.additional],
                         catalog_version=data.catalog.catalog_version,
                         verification=Verification(confirmed_by_user=bool(body.confirmed_ktru_code)))
    cards = getattr(components.extractor, 'cards', None)
    result.card_schema_version = cards.version if cards else None
    result.card_schema_evidence = list(card_evidence)
    result.original_facts = [fact.model_copy(deep=True) for fact in extracted]
    if extraction:
        result.corrections.extend(extraction.corrections)
    registry = {property_question_id(category_id, prop): prop for position in positions for prop in position.properties}
    answered = {answer.question_id for answer in body.answers}
    for answer in body.answers:
        prop = registry.get(answer.question_id)
        previous_prop = data.answer_properties.get(answer.question_id) if not prop else None
        if previous_prop:
            extracted.append(Fact(id='answer:' + answer.question_id, name=previous_prop.name,
                value=answer.value or '', values=answer.values, unit=answer.unit,
                raw=json.dumps(answer.values, ensure_ascii=False) if answer.values is not None else answer.value or 'не знаю',
                source='answer', subject=previous_prop.subject,
                unknown=answer.unknown or (answer.values is None and not answer.value)))
        if prop:
            from app.search import canon
            superseded = [fact for fact in extracted if fact.subject == prop.subject and canon(fact.name) in
                          {canon(prop.name), *(canon(alias) for alias in prop.aliases)}]
            extracted = [fact for fact in extracted if fact not in superseded]
            for fact in superseded:
                result.corrections.append(Correction(fact_id="answer:" + answer.question_id, kind="proposed_fact_change",
                                                    original=fact.raw, normalized=json.dumps(answer.values, ensure_ascii=False) if answer.values is not None else answer.value or "не знаю",
                                                    explanation="Текущий явный ответ пользователя заменяет предыдущее значение; источник сохранён"))
            extracted.append(Fact(id="answer:" + answer.question_id, name=prop.name, value=answer.value or "",
                                  values=answer.values, unit=answer.unit or property_unit(prop),
                                  raw=json.dumps(answer.values, ensure_ascii=False) if answer.values is not None else answer.value or "не знаю", source="answer", subject=prop.subject,
                                  unknown=answer.unknown or (answer.values is None and not answer.value)))
    result.facts = apply_inputs(extracted, body.attributes)
    auto_single_values(result, positions, result.facts)
    if hasattr(components.extractor, 'normalize_context'):
        components.extractor.normalize_context(result.facts, category_id, extraction_properties)
    for fact in result.facts:
        fact.card_mappings = [prop.id for prop in card_properties if prop.name == fact.name and prop.subject == fact.subject]
        fact.extraction_evidence = list(dict.fromkeys([*fact.extraction_evidence,
            *(prop.evidence for prop in card_properties if prop.id in fact.card_mappings)]))
    # A current property answer is the full replacement, also for edited card
    # inputs. kind=3 must not silently union an obsolete selection with it.
    for answer in body.answers:
        prop = registry.get(answer.question_id)
        if prop is None or any(item.replaces == "answer:" + answer.question_id for item in body.attributes):
            continue
        from app.search import canon
        names = {canon(prop.name), *(canon(alias) for alias in prop.aliases)}
        superseded = [fact for fact in result.facts if fact.source == "input" and
                      fact.subject == prop.subject and canon(fact.name) in names]
        result.facts = [fact for fact in result.facts if fact not in superseded]
        for fact in superseded:
            result.corrections.append(Correction(fact_id="answer:" + answer.question_id,
                kind="proposed_fact_change", original=fact.raw,
                normalized=json.dumps(answer.values, ensure_ascii=False) if answer.values is not None else answer.value or "не знаю",
                explanation="Текущий ответ заменяет прежний выбор карточки; все значения перепроверены"))
    sources = {fact.id: fact.raw for fact in result.original_facts + extracted} | {fragment.id: fragment.raw for fragment in unparsed}
    # Previous-category answers remain provenance for explicit edits, never proof in the new category.
    sources.update({"answer:" + answer.question_id: json.dumps(answer.values, ensure_ascii=False) if answer.values is not None else answer.value or "не знаю" for answer in body.answers
                    if answer.question_id in data.answer_properties})
    for item in body.attributes:
        if item.replaces:
            if item.replaces not in sources:
                raise InvalidConfirmation("attributes.replaces", "stale_source")
            result.corrections.append(Correction(fact_id=item.id, kind="proposed_fact_change", original=sources[item.replaces],
                                                normalized=f"{item.name}: {json.dumps(item.values, ensure_ascii=False) if item.values is not None else item.value} {item.unit or ''}".strip(),
                                                explanation="Явная правка пользователя; новые значения проверяются заново"))
    replaced = {item.replaces for item in body.attributes if item.replaces}
    result.unparsed = [fragment for fragment in unparsed if fragment.id not in replaced]
    pending, rejected, accepted = resolve_suggestions(category_id, positions, result.facts, body.answers,
                                                     extra_properties=card_properties)
    result.corrections.extend(accepted)
    clear_unknown_properties(result, positions, body.answers, registry)
    if search:
        finder = getattr(search, "find_state", ExistingSearcher(search).find_state)
        result.categories = finder(SearchInput(body.description, tuple(result.facts)))
        if any(category.id not in data.categories for category in result.categories):
            raise ValueError("Component returned foreign category ID")
    if data.problem or components.missing or not data.categories or not data.positions or not search:
        reason = data.problem or (components.missing[0] + "_missing" if components.missing else "search_unavailable" if not search else "catalog_empty")
        result.issues = [Issue(code=reason, explanation=f"Полный анализ не готов: {reason}",
                              suggested_action="Подготовьте проверенный snapshot и перезапустите API")]
        result.next_steps = ["Подключите недостающие компоненты: " + ", ".join((*data.missing_resources, *components.missing) or (reason,))]
        if body.confirmed_ktru_code and body.confirmed_ktru_code in data.positions:
            position = data.positions[body.confirmed_ktru_code]
            if position.is_template is True:
                raise InvalidConfirmation("confirmed_ktru_code", "template_confirmation")
            if position.source_issues or position.resolve_blocked is True:
                result.issues.append(Issue(code="source_issue" if position.source_issues else "resolve_blocked",
                                          candidate_code=position.code, explanation="Источник запрещает завершение подбора этой позиции"))
        result.verification.blocking_issues = any(issue.severity == "error" for issue in result.issues)
        return result
    if not category_id:
        result.status = "needs_input"
        result.next_question, _ = components.questions(None, result.categories, (), [], result.facts, [], answered, False, None)
        result.next_steps = ["Подтвердите категорию; совпадение чисел не доказывает тип товара"]
        if not result.categories:
            # Поиск не нашёл значимых совпадений: показывать «пять похожих» нельзя,
            # иначе на товар, которого в КТРУ нет, выводился случайный набор.
            active = [issue for issue in result.issues if issue.code != "no_similar_products"]
            result.issues = [*active, Issue(code="no_similar_products", severity="warning",
                                            explanation="Похожих типов товара в КТРУ не найдено",
                                            suggested_action="Уточните описание: укажите тип товара другими словами "
                                                             "или добавьте существенные характеристики")]
            result.next_question = None
            result.next_steps = ["Ничего похожего не найдено — уточните описание товара"]
        if body.confirmed_ktru_code:
            result.issues.append(Issue(code="type_unconfirmed", explanation="Сначала подтвердите категорию товара"))
        return result
    result.scope = Scope(category_id=category_id, position_count=len(positions), evaluated_count=len(positions), complete=scope_complete)
    all_candidates = []
    all_issues = []
    catalog_blocked = set()
    for position in positions:
        candidate, issues, corrections = components.checker.evaluate(position, result.facts, data.resolve_unit)
        if candidate.ktru_code != position.code:
            raise ValueError("Component returned foreign candidate ID")
        checks = {check.property_id: check for check in candidate.checks}
        required_missing = [prop.name for prop in position.properties if prop.required is True and
                            (prop.id not in checks or checks[prop.id].outcome != "compatible" or not checks[prop.id].fact_ids)]
        if required_missing:
            candidate.missing_required = sorted(set(candidate.missing_required) | set(required_missing))
            if candidate.outcome == "compatible":
                candidate.outcome = "unknown"
        candidate.blocking_reasons = position_blockers(position, candidate.checks)
        active_blockers = [reason for reason in candidate.blocking_reasons if reason.blocks_resolution]
        if active_blockers:
            catalog_blocked.add(position.code)
            if candidate.outcome != "incompatible":
                candidate.outcome = "unknown"
            issues.extend(Issue(code=reason.code, candidate_code=position.code,
                property_id=reason.property_id, rule_source=reason.rule_source, explanation=reason.explanation,
                suggested_action="Нужно поддержать семантику ограничения или уточнить источник; заполнение формы не снимает блокировку")
                for reason in active_blockers)
        contradictory = {check.property_id for check in candidate.checks if check.outcome == "incompatible"}
        if candidate.outcome == "incompatible":
            candidate.exclusion_reasons = [issue for issue in issues if issue.property_id in contradictory
                                           and issue.candidate_code == position.code]
        all_candidates.append(candidate)
        all_issues.extend(issues)
        result.corrections.extend(corrections)
    remaining = [candidate for candidate in all_candidates if candidate.outcome != "incompatible"]
    excluded = [candidate for candidate in all_candidates if candidate.outcome == "incompatible"]
    result.candidate_count, result.excluded_count = len(remaining), len(excluded)
    result.candidates = remaining[:body.candidate_limit]
    result.excluded = excluded[:body.candidate_limit]
    active_issues = [issue for issue in all_issues if issue.candidate_code in {candidate.ktru_code for candidate in remaining}]
    result.corrections = [correction for correction in result.corrections
                          if not (correction.kind == "proposed_text_fix" and correction.fact_id in rejected)]
    if not remaining:
        active_issues = all_issues + [Issue(code="no_compatible_candidate", explanation="В выбранной области нет совместимых позиций")]
    active_issues.extend(item[2] for item in pending)
    if body.confirmed_ktru_code:
        confirmed = next((candidate for candidate in remaining if candidate.ktru_code == body.confirmed_ktru_code), None)
        if confirmed is None:
            raise InvalidConfirmation("confirmed_ktru_code", "incompatible_ktru_code")
        if confirmed not in result.candidates:
            result.candidates = [*result.candidates[:body.candidate_limit - 1], confirmed]
    result.additional.extend(Fragment(raw=f"{fact.name}: {fact.raw if fact.values is not None else fact.value} {fact.unit or ''}".strip(), span=fact.span)
                             for fact in result.facts if not fact.mappings)
    if not scope_complete:
        active_issues.append(Issue(code="insufficient_catalog_coverage", explanation="Полнота связей и конечных позиций выбранной области не подтверждена"))
    if any(match["unit"] for fragment in result.unparsed for match in MEASUREMENT.finditer(fragment.raw)):
        active_issues.append(Issue(code="unmapped_measurement", explanation="В исходном тексте осталось измерение без установленного свойства",
                                  suggested_action="Добавьте характеристику с именем и единицей или уточните описание"))
    if any(fact.unit and not fact.name and fact.id not in rejected for fact in result.facts):
        active_issues.append(Issue(code='unmapped_measurement', explanation='Измерение сохранено в карточке, но его свойство не установлено',
                                  suggested_action='Уточните назначение измерения либо сохраните как дополнительные сведения'))
    if any(candidate.outcome == "unknown" for candidate in remaining):
        active_issues.append(Issue(code="verification_incomplete", explanation="Есть непроверенные факты, обязательные поля или ограничения"))
    active_issues.extend(implausible_measurements(data, positions, remaining, result.facts, rejected))
    active_issues.extend(out_of_scale_values(data, result.facts, rejected,
                                             {group_prefix(candidate.ktru_code) for candidate in remaining}))
    unique = len(remaining) == 1
    value_verified = unique and remaining[0].outcome == "compatible" and not pending
    fields_complete = unique and not remaining[0].missing_required
    blocking = any(issue.severity == "error" for issue in active_issues)
    ready = unique and value_verified and fields_complete and scope_complete and not blocking
    # Ответ с недопустимым значением не считается отвеченным: вопрос задаётся снова, уже с
    # допустимыми границами из найденных позиций, иначе пользователь не узнает, что ввёл
    # значение вне диапазона, и поток вставал без вопроса.
    rejected_answers = {issue.fact_id.removeprefix("answer:") for issue in active_issues
                        if issue.fact_id and issue.fact_id.startswith("answer:")
                        and issue.code in INVALID_ANSWER_CODES}
    asked = {item for item in answered if item not in rejected_answers}
    result.next_question, indistinguishable = components.questions(category_id, result.categories, positions, remaining,
                                                           result.facts, active_issues, asked, ready, body.confirmed_ktru_code)
    if rejected_answers or any(issue.severity == "error" and issue.code in
                               ("value_out_of_scale", "implausible_value", "out_of_range")
                               for issue in active_issues):
        # Значение недопустимо или неправдоподобно: спрашиваем его, а не идём дальше.
        repeated = reask_after_invalid(category_id, positions, result.facts, active_issues)
        if repeated is not None and repeated[1].id in asked:
            # На этот вопрос уже отвечали (в том числе «Не знаю»): второй раз не спрашиваем,
            # иначе один и тот же экран возвращался бесконечно. Значение, помеченное как
            # неизвестное, перестаёт блокировать подтверждение.
            for issue in active_issues:
                if issue.code in ("value_out_of_scale", "implausible_value") and issue.severity == "error":
                    issue.severity = "warning"
                    issue.suggested_action = "Значение помечено как неизвестное; проверьте единицу или укажите число"
            repeated = None
        if repeated is not None:
            head, result.next_question = repeated
            result.next_steps = [result.next_question.title, head.suggested_action]
            # Справочник может не давать границ («900 и более мм»): берём наблюдаемые у
            # найденных групп, в единице карточки, иначе подсказка выглядит как «Допустимо: мм».
            if not any(item.lower is not None or item.upper is not None for item in result.next_question.ranges):
                fact = next((item for item in result.facts if item.id == head.fact_id), None)
                found = observed_range(data, getattr(fact, "name", "") or "",
                                       {group_prefix(candidate.ktru_code) for candidate in remaining})
                if found is not None:
                    result.next_question.ranges = [found]
                    result.next_question.hint = ranges_hint([found])
    if pending:
        result.next_question = next((entry[1] for entry in pending if entry[1] is not None), result.next_question)
        indistinguishable = False
    # Users can still complete the useful card. A code question requires proof;
    # unavailable semantics never bypasses ready / resolved conditions.
    if not pending and any(candidate.ktru_code in catalog_blocked for candidate in remaining):
        if result.next_question and result.next_question.kind == "code":
            result.next_question = None
    result.verification = Verification(type_verified=bool(category_id and data.categories[category_id].link_verified),
                                      ktru_unique=unique and value_verified, fields_complete=fields_complete, value_verified=value_verified,
                                      catalog_scope_complete=scope_complete, value_indistinguishable=indistinguishable,
                                      confirmed_by_user=bool(body.confirmed_ktru_code), blocking_issues=blocking)
    result.issues = list({issue.model_dump_json(): issue for issue in active_issues}.values())
    result.issues = merge_answer_issues(result.issues)
    result.corrections = list({item.model_dump_json(): item for item in result.corrections}.values())
    if ready and result.verification.type_verified and body.confirmed_ktru_code == remaining[0].ktru_code:
        result.status, result.selected_ktru_code = "resolved", remaining[0].ktru_code
        result.next_question = None
    else:
        result.status = "ambiguous" if len(remaining) > 1 and not blocking else "needs_input"
        if indistinguishable:
            result.status = "ambiguous"
            result.next_steps = ["Ограничения позиций одинаковы. Без проверенного смыслового разделителя выбрать один код нельзя"]
        elif result.next_question:
            result.next_steps = [result.next_question.title]
        else:
            result.next_steps = ["Проверьте замечания и уточните исходные сведения; неизвестное не считается подтверждённым"]
        if not result.next_question and any(candidate.ktru_code in catalog_blocked for candidate in remaining):
            result.status = "needs_data"
            result.issues.append(Issue(code="catalog_constraints_unavailable",
                explanation="В оставшемся пуле есть неподдержанные или непроверенные ограничения; правка ввода не устраняет эту блокировку",
                suggested_action="Нужна поддержка указанной семантики или уточнение данных источника; можно выбрать другой тип товара"))
            result.next_steps = ["Нужна поддержка семантики ограничений либо уточнение исходных единиц/данных; можно изменить категорию товара. Ручное подтверждение кода не обходит эту проверку"]
    return result
