"""Stateless A -> B -> contextual mapping -> C/E -> D orchestration."""
from app.catalog import Snapshot
from app.checks import evaluate
from app.extraction import MEASUREMENT, apply_inputs, extract, normalize
from app.questions import next_question, property_question_id
from app.schemas import AnalysisCreate, AnalysisOut, Correction, Fact, Fragment, Issue, Scope, Verification


class InvalidConfirmation(ValueError):
    def __init__(self, field: str, code: str):
        self.field, self.code = field, code


def analyze(data: Snapshot, search, body: AnalysisCreate) -> AnalysisOut:
    extracted, unparsed = extract(body.description, data.extraction_names)
    result = AnalysisOut(status="needs_data", original_text=body.description, normalized_text=normalize(body.description),
                         facts=extracted, unparsed=unparsed, additional=[Fragment(raw=raw) for raw in body.additional],
                         catalog_version=data.catalog.catalog_version,
                         verification=Verification(confirmed_by_user=bool(body.confirmed_ktru_code)))
    result.original_facts = [fact.model_copy(deep=True) for fact in extracted]
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
    if search:
        result.categories = search.find(body.description)
    if data.problem or not data.categories or not data.positions:
        result.issues = [Issue(code=data.problem or "catalog_empty", explanation="Проверенная поставка каталога, индексов и модели ещё не готова",
                              suggested_action="Подготовьте проверенный snapshot и перезапустите API")]
        result.next_steps = ["Сведения сохранены в форме. Подбор доступен после подключения проверенного каталога"]
        return result
    if not category_id:
        result.status = "needs_input"
        result.next_question, _ = next_question(None, result.categories, (), [], result.facts, [], answered, False, None)
        result.next_steps = ["Подтвердите категорию; совпадение чисел не доказывает тип товара"]
        if body.confirmed_ktru_code:
            result.issues.append(Issue(code="type_unconfirmed", explanation="Сначала подтвердите категорию товара"))
        return result
    result.scope = Scope(category_id=category_id, position_count=len(positions), evaluated_count=len(positions), complete=scope_complete)
    all_candidates = []
    all_issues = []
    for position in positions:
        candidate, issues, corrections = evaluate(position, result.facts)
        all_candidates.append(candidate)
        all_issues.extend(issues)
        result.corrections.extend(corrections)
    remaining = [candidate for candidate in all_candidates if candidate.outcome != "incompatible"]
    excluded = [candidate for candidate in all_candidates if candidate.outcome == "incompatible"]
    result.candidate_count, result.excluded_count = len(remaining), len(excluded)
    result.candidates = remaining[:body.candidate_limit]
    result.excluded = excluded[:body.candidate_limit]
    active_issues = [issue for issue in all_issues if issue.candidate_code in {candidate.ktru_code for candidate in remaining}]
    if not remaining:
        active_issues = all_issues + [Issue(code="no_compatible_candidate", explanation="В выбранной области нет совместимых позиций")]
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
    value_verified = unique and remaining[0].outcome == "compatible"
    fields_complete = unique and not remaining[0].missing_required
    blocking = any(issue.severity == "error" for issue in active_issues)
    ready = unique and value_verified and fields_complete and scope_complete and not blocking
    result.next_question, indistinguishable = next_question(category_id, result.categories, positions, remaining,
                                                           result.facts, active_issues, answered, ready, body.confirmed_ktru_code)
    result.verification = Verification(type_verified=bool(category_id and data.categories[category_id].link_verified),
                                      ktru_unique=unique, fields_complete=fields_complete, value_verified=value_verified,
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
    return result
