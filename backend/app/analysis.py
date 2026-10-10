"""Stateless A -> B -> contextual mapping -> C/E -> D orchestration."""
from app.catalog import Snapshot
from app.components import Components, ExistingSearcher, SearchInput, existing_components
from app.extraction import MEASUREMENT, apply_inputs, normalize
from app.questions import property_question_id
from app.correction_flow import resolve_suggestions
from app.schemas import AnalysisCreate, AnalysisOut, Correction, Fact, Fragment, Issue, Scope, Verification


class InvalidConfirmation(ValueError):
    def __init__(self, field: str, code: str):
        self.field, self.code = field, code


def analyze(data: Snapshot, search, body: AnalysisCreate, components: Components | None = None) -> AnalysisOut:
    components = components or existing_components()
    extraction = components.extractor.extract(body.description, data.extraction_names) if components.extractor else None
    extracted, unparsed = (extraction.facts, extraction.unparsed) if extraction else ([], [Fragment(raw=body.description)])
    result = AnalysisOut(status="needs_data", original_text=body.description, normalized_text=normalize(body.description),
                         facts=extracted, unparsed=unparsed, additional=[Fragment(raw=raw) for raw in body.additional],
                         catalog_version=data.catalog.catalog_version,
                         verification=Verification(confirmed_by_user=bool(body.confirmed_ktru_code)))
    result.original_facts = [fact.model_copy(deep=True) for fact in extracted]
    if extraction:
        result.corrections.extend(extraction.corrections)
    category_id = body.confirmed_category_id
    if category_id and category_id not in data.categories:
        raise InvalidConfirmation("confirmed_category_id", "unknown_category")
    positions, scope_complete = data.pool(category_id) if category_id else ((), False)
    registry = {property_question_id(category_id, prop): prop for position in positions for prop in position.properties}
    answered = {answer.question_id for answer in body.answers}
    for answer in body.answers:
        prop = registry.get(answer.question_id)
        if prop:
            from app.search import canon
            superseded = [fact for fact in extracted if fact.subject == prop.subject and canon(fact.name) in
                          {canon(prop.name), *(canon(alias) for alias in prop.aliases)}]
            extracted = [fact for fact in extracted if fact not in superseded]
            for fact in superseded:
                result.corrections.append(Correction(fact_id="answer:" + answer.question_id, kind="proposed_fact_change",
                                                    original=fact.raw, normalized=answer.value or "не знаю",
                                                    explanation="Текущий явный ответ пользователя заменяет предыдущее значение; источник сохранён"))
            extracted.append(Fact(id="answer:" + answer.question_id, name=prop.name, value=answer.value or "", unit=answer.unit,
                                  raw=answer.value or "не знаю", source="answer", subject=prop.subject,
                                  unknown=answer.unknown or not answer.value))
    result.facts = apply_inputs(extracted, body.attributes)
    sources = {fact.id: fact.raw for fact in result.original_facts + extracted} | {fragment.id: fragment.raw for fragment in unparsed}
    # Previous-category answers remain provenance for explicit edits, never proof in the new category.
    sources.update({"answer:" + answer.question_id: answer.value or "не знаю" for answer in body.answers
                    if answer.question_id in data.answer_properties})
    for item in body.attributes:
        if item.replaces:
            if item.replaces not in sources:
                raise InvalidConfirmation("attributes.replaces", "stale_source")
            result.corrections.append(Correction(fact_id=item.id, kind="proposed_fact_change", original=sources[item.replaces],
                                                normalized=f"{item.name}: {item.value} {item.unit or ''}".strip(),
                                                explanation="Явная правка пользователя; новые значения проверяются заново"))
    replaced = {item.replaces for item in body.attributes if item.replaces}
    result.unparsed = [fragment for fragment in unparsed if fragment.id not in replaced]
    pending, rejected, accepted = resolve_suggestions(category_id, positions, result.facts, body.answers)
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
        unsupported = any(not prop.verified or not prop.allowed or prop.required is None or
                          prop.kind not in ("1", "2") or prop.unsupported_reasons for prop in position.properties)
        if not position.properties or unsupported or position.source_issues or position.resolve_blocked is True:
            catalog_blocked.add(position.code)
            candidate.outcome = "unknown"
            issues.append(Issue(code="source_issue" if position.source_issues else "verification_incomplete",
                                candidate_code=position.code, explanation="Данные позиции не допускают завершение подбора"))
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
    result.additional.extend(Fragment(raw=f"{fact.name}: {fact.value} {fact.unit or ''}".strip(), span=fact.span)
                             for fact in result.facts if not fact.mappings)
    if not scope_complete:
        active_issues.append(Issue(code="insufficient_catalog_coverage", explanation="Полнота связей и конечных позиций выбранной области не подтверждена"))
    if any(match["unit"] for fragment in result.unparsed for match in MEASUREMENT.finditer(fragment.raw)):
        active_issues.append(Issue(code="unmapped_measurement", explanation="В исходном тексте осталось измерение без установленного свойства",
                                  suggested_action="Добавьте характеристику с именем и единицей или уточните описание"))
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
        result.next_question, indistinguishable = pending[0][1], False
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
                explanation="В оставшемся пуле есть позиция без пригодных проверенных ограничений; правка ввода не устраняет эту проблему каталога",
                suggested_action="Нужна исправленная поставка ограничений либо выбор другого типа товара"))
            result.next_steps = ["Нужна исправленная поставка ограничений; можно изменить категорию товара. Ручное подтверждение кода не обходит эту проверку"]
    return result
