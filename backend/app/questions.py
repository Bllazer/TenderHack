import hashlib

from app.catalog import Position, Property
from app.checks import rule_signature
from app.schemas import Candidate, CategoryCandidate, Fact, Issue, Question, QuestionOption


def property_question_id(category_id: str, prop: Property) -> str:
    identity = f"{category_id}/{prop.semantic_key or prop.id}/{prop.subject}"
    return "property:" + hashlib.sha256(identity.encode()).hexdigest()[:24]


def property_question(category_id: str, prop: Property, reason: str) -> Question:
    values = sorted({item.value for item in prop.allowed if item.kind == "enum" and item.value})
    units = {item.unit for item in prop.allowed}
    return Question(id=property_question_id(category_id, prop), kind="property", title=f"Укажите: {prop.name}",
                    property_name=prop.name, reason=reason,
                    options=[QuestionOption(value=value, label=value) for value in values[:20]],
                    unit=next(iter(units)) if len(units) == 1 else None)


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
