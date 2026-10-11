"""Property identity is contextual; lexical similarity is only a suggestion."""
from difflib import SequenceMatcher, get_close_matches

from app.catalog import Position, Property
from app.extraction import TYPO_SUGGEST
from app.schemas import Fact, Issue, Correction
from app.search import canon


def match_properties(position: Position, name: str, subject: str) -> list[Property]:
    """Свойства позиции, чьё имя или алиас совпадает с именем факта в том же субъекте.

    Сопоставление по нормализованному имени: лексическое совпадение — только
    подсказка, идентичность свойства контекстна. Используется и привязкой фактов,
    и разрешением def_id в app.structured.
    """
    target = canon(name)
    return [prop for prop in position.properties if subject == prop.subject
            and target in {canon(prop.name), *(canon(alias) for alias in prop.aliases)}]


def map_facts(position: Position, facts: list[Fact]):
    groups: dict[str, list[Fact]] = {prop.id: [] for prop in position.properties}
    issues: list[Issue] = []
    corrections: list[Correction] = []
    names = [prop.name for prop in position.properties]
    for fact in facts:
        matches = match_properties(position, fact.name, fact.subject)
        if matches:
            priority = min(0 if prop.source == "ktru" else 1 for prop in matches)
            matches = [prop for prop in matches if (0 if prop.source == "ktru" else 1) == priority]
        if len(matches) == 1:
            prop: Property = matches[0]
            groups[prop.id].append(fact)
            identity = f"{position.code}/{prop.id}"
            if identity not in fact.mappings:
                fact.mappings.append(identity)
        elif len(matches) > 1:
            issues.append(Issue(code="mapping_ambiguous", fact_id=fact.id, candidate_code=position.code,
                                explanation=f"Имя «{fact.name}» соответствует нескольким свойствам позиции; уточните контекст"))
        elif fact.name:
            # Порог тот же, что у подсказки в извлечении (`TYPO_SUGGEST`), иначе факт
            # с опечаткой создавался бы, но вопроса о нём не возникало.
            possible = [] if fact.card_mappings else get_close_matches(
                canon(fact.name), [canon(prop.name) for prop in position.properties
                                   if prop.subject == fact.subject], n=1, cutoff=TYPO_SUGGEST)
            if possible:
                target = next(name for name in names if canon(name) == possible[0])
                seen = SequenceMatcher(None, canon(fact.name), canon(target)).ratio()
                corrections.append(Correction(fact_id=fact.id, kind="proposed_text_fix", original=fact.name,
                                              normalized=target,
                                              explanation=f"Возможно, вы имели в виду «{target}»: "
                                                          f"совпадение написания {seen:.0%}; "
                                                          "исправление не применено без вашего ответа",
                                              confirmation_required=True))
            if position.no_new_features is True:
                issues.append(Issue(code="additional_not_exportable", severity="warning", fact_id=fact.id,
                                    candidate_code=position.code, rule_source=position.no_new_features_evidence,
                                    explanation=f"«{fact.name}» сохранено дополнительно; у позиции запрещено добавление характеристик"))
    return groups, issues, corrections
