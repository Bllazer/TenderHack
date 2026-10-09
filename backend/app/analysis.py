"""Deterministic catalog baseline. Replace this function with a model adapter later."""

import re
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Attribute, Category, CategoryAttribute
from app.schemas import AnalysisResult, Candidate, InputAttribute, Issue, NormalizedAttribute


def canon(text: str) -> str:
    return " ".join(text.casefold().replace("ё", "е").split())


def analyze(session: Session, attributes: list[InputAttribute]) -> tuple[str, AnalysisResult]:
    categories = session.scalars(select(Category).where(Category.is_leaf.is_(True)).order_by(Category.ktru_code)).all()
    if not categories:
        return "needs_data", AnalysisResult(
            issues=[Issue(field="catalog", code="catalog_empty", message="Справочник КТРУ ещё не загружен")],
            next_steps=["Загрузить справочник категорий и характеристик"],
        )

    definitions = session.scalars(select(Attribute)).all()
    by_name = {canon(alias): item for item in definitions for alias in [item.name, *item.aliases]}
    normalized: list[NormalizedAttribute] = []
    issues: list[Issue] = []
    seen: set[str] = set()
    for index, field in enumerate(attributes):
        definition = by_name.get(canon(field.name))
        if definition is None:
            issues.append(Issue(field=f"attributes.{index}.name", code="unknown_attribute", message=f"Не найдена характеристика «{field.name}»"))
            continue
        if definition.id in seen:
            issues.append(Issue(field=f"attributes.{index}.name", code="duplicate_attribute", message="Характеристика указана дважды"))
            continue
        seen.add(definition.id)
        if field.unit is not None and canon(definition.unit or "") != canon(field.unit):
            issues.append(Issue(field=f"attributes.{index}.unit", code="unit_mismatch", message=f"Ожидаемая единица: {definition.unit}; преобразование не выполнено"))
            continue
        if definition.value_type == "number" and not re.fullmatch(r"[+-]?\d+(?:[.,]\d+)?", field.value.strip()):
            issues.append(Issue(field=f"attributes.{index}.value", code="invalid_number", message="Ожидается числовое значение"))
            continue
        normalized.append(NormalizedAttribute(
            attribute_id=definition.id, name=definition.name,
            value=field.value.strip().replace(",", ".") if definition.value_type == "number" else field.value.strip(),
            unit=definition.unit or field.unit,
        ))

    links = session.scalars(select(CategoryAttribute)).all()
    rules: dict[str, list[CategoryAttribute]] = {}
    for link in links:
        rules.setdefault(link.category_id, []).append(link)
    known = {item.id: item for item in definitions}
    values = {item.attribute_id: item.value for item in normalized}
    candidates: list[Candidate] = []
    for category in categories:
        category_rules = rules.get(category.id, [])
        allowed_ids = {rule.attribute_id for rule in category_rules}
        # Every recognized user attribute must belong to the candidate category.
        if any(attribute_id not in allowed_ids for attribute_id in values):
            continue
        if any(rule.attribute_id in values and rule.allowed_values and
               not value_allowed(known[rule.attribute_id], values[rule.attribute_id], rule.allowed_values)
               for rule in category_rules):
            continue
        matched = [known[key].name for key in values if key in allowed_ids]
        missing = [known[rule.attribute_id].name for rule in category_rules
                   if rule.required and rule.attribute_id not in values]
        candidates.append(Candidate(category_id=category.id, ktru_code=category.ktru_code,
                                    name=category.name, matched_attributes=matched, missing_required=missing))

    candidates.sort(key=lambda item: (len(item.missing_required), item.ktru_code))
    candidates = candidates[:20]
    result = AnalysisResult(normalized_attributes=normalized, candidates=candidates, issues=issues)
    if not candidates:
        result.issues.append(Issue(field="attributes", code="no_match", message="Совместимый код КТРУ не найден"))
        result.next_steps = ["Проверьте названия, значения и единицы характеристик"]
        return "needs_input", result
    if len(candidates) == 1 and not candidates[0].missing_required and not issues and normalized:
        result.selected_ktru_code = candidates[0].ktru_code
        return "resolved", result
    missing = sorted({name for candidate in candidates for name in candidate.missing_required})
    result.next_steps = ([f"Уточните характеристику: {name}" for name in missing[:5]] or
                         ["Добавьте характеристику, различающую варианты КТРУ"])
    return ("ambiguous" if len(candidates) > 1 and not issues else "needs_input"), result


def value_allowed(attribute: Attribute, value: str, allowed: list[str]) -> bool:
    if attribute.value_type == "number":
        try:
            return any(Decimal(value) == Decimal(item.strip().replace(",", ".")) for item in allowed)
        except InvalidOperation:
            return False
    return canon(value) in {canon(item) for item in allowed}
