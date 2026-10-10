"""Stateless A -> B -> contextual mapping -> C/E -> D orchestration."""
import json
from app.catalog import Snapshot
from app.components import Components, ExistingSearcher, SearchInput, existing_components
from app.extraction import MEASUREMENT, apply_inputs, normalize
from app.questions import property_question_id
from app.correction_flow import resolve_suggestions
from app.catalog_diagnostics import position_blockers
from app.schemas import AnalysisCreate, AnalysisOut, Correction, Fact, Fragment, Issue, Scope, Verification


class InvalidConfirmation(ValueError):
    def __init__(self, field: str, code: str):
        self.field, self.code = field, code


def analyze(data: Snapshot, search, body: AnalysisCreate, components: Components | None = None) -> AnalysisOut:
    components = components or existing_components()
    category_id = body.confirmed_category_id
    if category_id and category_id not in data.categories:
        raise InvalidConfirmation("confirmed_category_id", "unknown_category")
    positions, scope_complete = data.pool(category_id) if category_id else ((), False)
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
            extracted.append(Fact(id="answer:" + answer.question_id, name=prop.name, value=answer.value or "", values=answer.values, unit=answer.unit,
                                  raw=json.dumps(answer.values, ensure_ascii=False) if answer.values is not None else answer.value or "не знаю", source="answer", subject=prop.subject,
                                  unknown=answer.unknown or (answer.values is None and not answer.value)))
    result.facts = apply_inputs(extracted, body.attributes)
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
    unique = len(remaining) == 1
    value_verified = unique and remaining[0].outcome == "compatible" and not pending
    fields_complete = unique and not remaining[0].missing_required
    blocking = any(issue.severity == "error" for issue in active_issues)
    ready = unique and value_verified and fields_complete and scope_complete and not blocking
    result.next_question, indistinguishable = components.questions(category_id, result.categories, positions, remaining,
                                                           result.facts, active_issues, answered, ready, body.confirmed_ktru_code)
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
