import hashlib
from decimal import Decimal, InvalidOperation

from app.catalog import Position, Property
from app.checks import rule_signature
from app.schemas import Candidate, CategoryCandidate, Fact, Issue, Question, QuestionOption, QuestionRange


def property_question_id(category_id: str, prop: Property) -> str:
    identity = f"{category_id}/{prop.semantic_key or prop.id}/{prop.subject}"
    return "property:" + hashlib.sha256(identity.encode()).hexdigest()[:24]


def _decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


#: Сколько значений характеристики ещё показываем выбором. У «Роста» детского халата их 23
#: (74…152 см) — это дискретный набор, как размер одежды, и писать его руками не нужно;
#: длинные перечни (больше 30) остаются полем ввода с подсказкой диапазона.
NUMERIC_OPTION_LIMIT = 30


def numeric_options(prop: Property) -> list[QuestionOption]:
    """Варианты числовой характеристики: отдельные значения и интервалы-разбиения.

    Справочник задаёт такие характеристики либо набором чисел (Рост: 74, 80, 86 …), либо
    разбиением на интервалы (Длина рукоятки: до 100, 100–150, … 900 и более). И то, и другое —
    закрытый набор, поэтому пользователю предлагается выбор, а не свободный ввод.
    """
    units = {item.unit for item in prop.allowed if item.unit}
    unit = next(iter(units)) if len(units) == 1 else None
    rows: list[tuple[float, str, str]] = []
    for item in prop.allowed:
        if item.kind == "number" and item.value not in (None, ""):
            rows.append((float(item.value), str(item.value), f"{item.value} {unit or ''}".strip()))
        elif item.kind == "interval" and (item.lower not in (None, "") or item.upper not in (None, "")):
            if item.lower in (None, ""):
                rows.append((float(item.upper), f"≤{item.upper}", f"до {item.upper} {unit or ''}".strip()))
            elif item.upper in (None, ""):
                rows.append((float(item.lower), f"≥{item.lower}", f"от {item.lower} {unit or ''}".strip()))
            else:
                value = f"{item.lower}–{item.upper}"
                rows.append((float(item.lower), value, f"{value} {unit or ''}".strip()))
    if not rows or len(rows) > NUMERIC_OPTION_LIMIT:
        return []
    rows.sort(key=lambda row: row[0])
    return [QuestionOption(value=value, label=label) for _, value, label in rows]


def allowed_ranges(prop: Property) -> list[QuestionRange]:
    """Объединение интервалов характеристики у оставшихся позиций, по единицам.

    Интервалы берутся из справочника (в `prop.allowed` уже лежат значения всех оставшихся
    позиций), поэтому границы всегда соответствуют найденным позициям и меняются вместе с
    ними. Точные числа справочника (Рост: 74…152) дают отрезок этих значений. Пересекающиеся
    интервалы сливаются: пользователю нужен список, а не набор пересечений. Разные единицы
    не смешиваются — для каждой своя запись.
    """
    by_unit: dict[str | None, list[tuple[Decimal, Decimal]]] = {}
    for item in prop.allowed:
        if item.kind == "number" and item.value not in (None, ""):
            value = _decimal(item.value)
            if value is not None:
                by_unit.setdefault(item.unit, []).append((value, value))
            continue
        if item.kind != "interval":
            continue
        lower, upper = _decimal(item.lower), _decimal(item.upper)
        if lower is None and upper is None:
            continue
        by_unit.setdefault(item.unit, []).append((lower if lower is not None else Decimal("-Infinity"),
                                                 upper if upper is not None else Decimal("Infinity")))
    ranges: list[QuestionRange] = []
    for unit, intervals in by_unit.items():
        merged: list[list[Decimal]] = []
        for lower, upper in sorted(intervals):
            if merged and lower <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], upper)
            else:
                merged.append([lower, upper])
        for lower, upper in merged:
            ranges.append(QuestionRange(
                lower=None if lower.is_infinite() else float(lower),
                upper=None if upper.is_infinite() else float(upper),
                unit=unit))
    return ranges


def ranges_hint(ranges: list[QuestionRange], unit: str | None = None) -> str | None:
    """Подсказка о допустимом значении; единица — из диапазона или из вопроса.

    Набор отдельных чисел (Рост: 74, 80, 86 …) не превращается в список «от 74 до 74, от 80
    до 80 …»: такие значения и так показываются выбором, поэтому подсказка говорит о границах
    набора.
    """
    exact = [item for item in ranges if item.lower is not None and item.lower == item.upper]
    if ranges and len(exact) == len(ranges):
        values = [item.lower for item in exact]
        if len(set(values)) == 1:
            # Одно возможное значение — так и пишем: «Допустимо: 13 мм», а не «от 13 до 13 мм».
            return f"Допустимо: {_short(values[0])} {unit or ''}".strip()
        return f"Допустимо одно из значений: от {_short(values[0])} до {_short(values[-1])} {unit or ''}".strip()
    texts = [item.text() for item in ranges
             if (item.lower is not None or item.upper is not None)]
    texts = [text if text else "" for text in texts]
    texts = [text for text in texts if text]
    if not texts:
        return None
    return "Допустимо: " + " или ".join(texts)


def _short(value: float | None) -> str:
    return "" if value is None else f"{value:g}"


def property_question(category_id: str, prop: Property, reason: str) -> Question:
    values = sorted({item.value for item in prop.allowed if item.kind == "enum" and item.value})
    units = {item.unit for item in prop.allowed}
    ranges = allowed_ranges(prop)
    # Перечисление — выбор из значений; закрытый числовой набор (рост, диапазоны длины) —
    # такой же выбор; всё остальное остаётся полем ввода с подсказкой диапазона.
    options = ([QuestionOption(value=value, label=value) for value in values[:20]] if values
               else numeric_options(prop))
    unit = next(iter(units)) if len(units) == 1 else None
    return Question(id=property_question_id(category_id, prop), kind="property", title=f"Укажите: {prop.name}",
                    property_name=prop.name, reason=reason, options=options,
                    unit=unit, multiple=prop.kind == "3",
                    ranges=ranges, hint=ranges_hint(ranges, unit))


def next_question(category_id: str | None, categories: list[CategoryCandidate], positions: tuple[Position, ...],
                  candidates: list[Candidate], facts: list[Fact], issues: list[Issue], answered: set[str],
                  unique_ready: bool, confirmed_code: str | None):
    conflict = next((issue for issue in issues if issue.code in ("conflicting_facts", "mapping_ambiguous")), None)
    if conflict:
        return Question(id="conflict:" + (conflict.fact_id or "mapping"), kind="conflict",
                        title="Исправьте противоречащие сведения", reason=conflict.explanation,
                        allow_unknown=False), False
    if not category_id:
        if "category" in answered:
            return None, False
        return Question(id="category", kind="category", title="Подтвердите тип товара",
                        reason="Ранжирование предлагает категории, но не подтверждает принадлежность товара",
                        options=[QuestionOption(value=category.id, label=category.name) for category in categories]), False
    pool = {position.code: position for position in positions}
    remaining = [pool[candidate.ktru_code] for candidate in candidates]
    signatures = [rule_signature(position) for position in remaining]
    indistinguishable = len(remaining) > 1 and signatures[0] is not None and all(item == signatures[0] for item in signatures)
    if indistinguishable:
        # Names/codes alone are not trusted semantic discriminators. No endless value questions.
        return None, True
    props = {(prop.semantic_key or prop.id, prop.subject): prop for position in remaining for prop in position.properties}
    choices = []
    for key, prop in props.items():
        qid = property_question_id(category_id, prop)
        if qid in answered or not prop.verified:
            continue
        checks = [check for candidate in candidates for check in candidate.checks
                  if check.property_id in {other.id for position in remaining for other in position.properties
                                           if (other.semantic_key or other.id, other.subject) == key}]
        if checks and all(check.outcome == "compatible" for check in checks):
            continue
        constraints = {tuple(item.model_dump_json() for item in other.allowed)
                       for position in remaining for other in position.properties if (other.semantic_key or other.id, other.subject) == key}
        differing = len(remaining) > 1 and len(constraints) > 1
        required = any(prop.name in candidate.missing_required for candidate in candidates)
        needs_unit = any(check.reason in ("unit_missing", "unit_unknown") for check in checks)
        if differing or required or needs_unit:
            variants = {item.model_dump_json(): item for position in remaining for other in position.properties
                        if (other.semantic_key or other.id, other.subject) == key and other.verified for item in other.allowed}
            choices.append((0 if differing else 1, prop.name, prop.model_copy(update={"allowed": tuple(variants.values())})))
    if choices:
        priority, _, prop = sorted(choices, key=lambda item: (item[0], item[1]))[0]
        return property_question(category_id, prop, "Различает позиции" if priority == 0 else "Нужно для проверки обязательного поля или единицы"), False
    if unique_ready and not confirmed_code:
        candidate = candidates[0]
        return Question(id="code", kind="code", title="Подтвердите конечный код КТРУ",
                        reason="Тип и значения проверены; требуется ваш явный выбор",
                        options=[QuestionOption(value=candidate.ktru_code, label=f"{candidate.name} — {candidate.ktru_code}")]), False
    return None, False
