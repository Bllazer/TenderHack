"""Three-valued constraint checking. Unknown is never success."""
import re
from dataclasses import dataclass
from decimal import Decimal

from app.catalog import AllowedValue, Position, Property
from app.mapping import map_facts
from app.schemas import Candidate, Check, Correction, Fact, Issue
from app.search import canon
from app.units import get_unit

NUM = r"[+-]?\d+(?:[.,]\d+)?"
INF = Decimal("Infinity")


@dataclass(frozen=True)
class Interval:
    lower: Decimal
    upper: Decimal
    lower_inclusive: bool = True
    upper_inclusive: bool = True

    def scaled(self, factor: Decimal):
        return Interval(self.lower * factor, self.upper * factor, self.lower_inclusive, self.upper_inclusive)


def number(value: str) -> Interval | None:
    value = canon(value).replace("−", "-").replace(",", ".")
    point = re.fullmatch(NUM, value)
    if point:
        return Interval(Decimal(value), Decimal(value))
    compare = re.fullmatch(rf"(<=|>=|<|>|≤|≥|не менее|не более|не ниже|не выше)\s*({NUM})", value)
    if compare:
        operator, bound = compare[1], Decimal(compare[2])
        if operator in (">", ">=", "≥", "не менее", "не ниже"):
            return Interval(bound, INF, operator != ">", False)
        return Interval(-INF, bound, False, operator != "<")
    bracket = re.fullmatch(rf"([\[(])\s*({NUM})\s*;\s*({NUM})\s*([\])])", value)
    if bracket:
        result = Interval(Decimal(bracket[2]), Decimal(bracket[3]), bracket[1] == "[", bracket[4] == "]")
    else:
        ranged = re.fullmatch(rf"({NUM})\s*(?:–|\.\.|…|-)\s*({NUM})", value)
        if not ranged:
            return None
        result = Interval(Decimal(ranged[1]), Decimal(ranged[2]))
    return result if result.lower <= result.upper else None


def allowed_interval(item: AllowedValue) -> Interval:
    if item.kind == "number":
        value = Decimal(item.value)
        return Interval(value, value)
    return Interval(Decimal(item.lower) if item.lower is not None else -INF,
                    Decimal(item.upper) if item.upper is not None else INF,
                    item.lower_inclusive, item.upper_inclusive)


def subset(query: Interval, allowed: list[Interval]) -> bool:
    # Union only after all alternatives have been converted to one verified dimension.
    merged: list[Interval] = []
    for interval in sorted(allowed, key=lambda item: (item.lower, not item.lower_inclusive)):
        if not merged:
            merged.append(interval)
            continue
        previous = merged[-1]
        join = interval.lower < previous.upper or (interval.lower == previous.upper and
                                                   (interval.lower_inclusive or previous.upper_inclusive))
        if join:
            upper = max(previous.upper, interval.upper)
            inclusive = ((previous.upper == upper and previous.upper_inclusive) or
                         (interval.upper == upper and interval.upper_inclusive))
            merged[-1] = Interval(previous.lower, upper, previous.lower_inclusive, inclusive)
        else:
            merged.append(interval)
    return any((query.lower > item.lower or (query.lower == item.lower and (item.lower_inclusive or not query.lower_inclusive)))
               and (query.upper < item.upper or (query.upper == item.upper and (item.upper_inclusive or not query.upper_inclusive)))
               for item in merged)


def signature(fact: Fact, prop: Property):
    if prop.value_type == "number":
        parsed, unit = number(fact.value), get_unit(fact.unit)
        if parsed:
            return parsed.scaled(unit.factor) if unit else parsed, unit.dimension if unit else fact.unit
    return canon(fact.value), canon(fact.unit or "")


def check_fact(prop: Property, fact: Fact):
    """outcome, reason, value_codes, optional safe normalization."""
    if fact.unknown:
        return "unknown", "unknown_fact", [], None
    if not prop.verified or not prop.evidence or prop.required is None or prop.value_type == "unsupported":
        return "unknown", "unsupported_constraint", [], None
    if not prop.allowed:
        return "unknown", "no_supported_constraints", [], None
    if prop.value_type in ("text", "boolean"):
        if any(item.kind != "enum" for item in prop.allowed):
            return "unknown", "unsupported_constraint", [], None
        value = canon(fact.value)
        boolean = {"да": "да", "есть": "да", "true": "да", "нет": "нет", "без": "нет", "false": "нет"}
        if prop.value_type == "boolean":
            if value not in boolean:
                return "incompatible", "invalid_type", [], None
            value = boolean[value]
        accepted = [item for item in prop.allowed if (boolean.get(canon(item.value), canon(item.value))
                    if prop.value_type == "boolean" else canon(item.value)) == value]
        if accepted:
            supplied_unit = get_unit(fact.unit)
            matches = [item for item in accepted if canon(item.unit or "") == canon(fact.unit or "") or
                       (supplied_unit is not None and get_unit(item.unit) == supplied_unit)]
            if not matches:
                if not fact.unit:
                    return "unknown", "unit_missing", [], None
                return "incompatible", "unit_mismatch", [], None
            accepted = matches
        return ("compatible", "allowed_value", [item.value_code for item in accepted if item.value_code], None) if accepted else (
            "incompatible", "invalid_value", [], None)
    parsed = number(fact.value)
    if parsed is None:
        return "incompatible", "invalid_number", [], None
    if any(item.kind == "enum" for item in prop.allowed):
        return "unknown", "unsupported_constraint", [], None
    units = [get_unit(item.unit) for item in prop.allowed]
    if any(item.unit and unit is None for item, unit in zip(prop.allowed, units)):
        return "unknown", "unsupported_constraint", [], None
    dimensions = {unit.dimension if unit else "dimensionless" for unit in units}
    if len(dimensions) != 1:
        return "unknown", "unsupported_constraint", [], None
    supplied = get_unit(fact.unit)
    expected_dimension = next(iter(dimensions))
    if expected_dimension != "dimensionless" and not fact.unit:
        return "unknown", "unit_missing", [], None
    if fact.unit and supplied is None:
        return "unknown", "unit_unknown", [], None
    if (supplied.dimension if supplied else "dimensionless") != expected_dimension:
        return "incompatible", "unit_mismatch", [], None
    physical = parsed.scaled(supplied.factor if supplied else Decimal(1))
    intervals = [allowed_interval(item).scaled(unit.factor if unit else Decimal(1)) for item, unit in zip(prop.allowed, units)]
    accepted = subset(physical, intervals)
    if not accepted:
        return "incompatible", "out_of_range", [], None
    target = units[0]
    converted = parsed.scaled(supplied.factor / target.factor) if supplied and target else parsed
    if converted.lower == converted.upper:
        normalized = format(converted.lower.normalize(), "f")
    else:
        normalized = f"{'[' if converted.lower_inclusive else '('}{converted.lower}; {converted.upper}{']' if converted.upper_inclusive else ')'}"
    contributors = [item for item, interval in zip(prop.allowed, intervals)
                    if physical.upper >= interval.lower and physical.lower <= interval.upper]
    return "compatible", "allowed_range", [item.value_code for item in contributors if item.value_code], (normalized, target.name if target else None)


def evaluate(position: Position, facts: list[Fact]):
    groups, issues, corrections = map_facts(position, facts)
    checks: list[Check] = []
    missing: list[str] = []
    has_checked_value = False
    for prop in position.properties:
        found = groups[prop.id]
        known = [fact for fact in found if not fact.unknown]
        if prop.required is True and not known:
            missing.append(prop.name)
        if len({signature(fact, prop) for fact in known}) > 1:
            issues.append(Issue(code="conflicting_facts", property_id=prop.id, candidate_code=position.code,
                                fact_id=known[0].id, rule_source=prop.evidence,
                                explanation=f"Для «{prop.name}» указаны противоречащие значения: " + ", ".join(fact.value for fact in known),
                                suggested_action="Исправьте конкретный факт или исходное описание", confirmation_required=True))
            checks.append(Check(property_id=prop.id, name=prop.name, outcome="unknown", reason="conflicting_facts",
                                fact_ids=[fact.id for fact in found]))
            continue
        if not found:
            reason = "missing_required" if prop.required else "not_provided"
            if not prop.verified or prop.required is None or not prop.allowed or prop.value_type == "unsupported":
                reason = "unsupported_constraint"
            checks.append(Check(property_id=prop.id, name=prop.name, outcome="unknown", reason=reason))
            continue
        results = [check_fact(prop, fact) for fact in found]
        outcome = ("incompatible" if any(row[0] == "incompatible" for row in results) else
                   "unknown" if any(row[0] == "unknown" for row in results) else "compatible")
        selected = next((row for row in results if row[0] == outcome), results[0])
        checks.append(Check(property_id=prop.id, name=prop.name, outcome=outcome, reason=selected[1],
                            fact_ids=[fact.id for fact in found], value_codes=sorted({code for row in results for code in row[2]})))
        has_checked_value |= outcome == "compatible"
        for fact, row in zip(found, results):
            if row[3]:
                normalized, unit = row[3]
                fact.normalized_value, fact.normalized_unit = normalized, unit
                if normalized != fact.value or (unit and unit != fact.unit):
                    corrections.append(Correction(fact_id=fact.id, kind="auto_normalization", original=f"{fact.value} {fact.unit or ''}".strip(),
                                                  normalized=f"{normalized} {unit or ''}".strip(), explanation="Безопасное преобразование в совместимой размерности"))
            if row[1] in ("invalid_value", "invalid_number", "invalid_type", "unit_mismatch", "out_of_range", "unit_missing", "unit_unknown", "unsupported_constraint"):
                issues.append(Issue(code=row[1], fact_id=fact.id, property_id=prop.id, candidate_code=position.code,
                                    rule_source=prop.evidence, severity="error" if row[0] != "compatible" else "info",
                                    explanation=explain(prop, fact, row[1]),
                                    suggested_action="Уточните значение или единицу по правилу позиции"))
    incompatible = any(check.outcome == "incompatible" for check in checks)
    # Unknown optional absent facts don't block, but unsupported definitions do.
    uncertain = any(check.outcome == "unknown" and check.reason != "not_provided" for check in checks)
    if not position.properties:
        checks.append(Check(property_id="", name="", outcome="unknown", reason="no_characteristics"))
        uncertain = True
    if any(issue.code == "mapping_ambiguous" for issue in issues):
        uncertain = True
    outcome = "incompatible" if incompatible else "unknown" if uncertain or not has_checked_value else "compatible"
    return Candidate(ktru_code=position.code, name=position.name, okpd2=position.okpd2, outcome=outcome,
                     checks=checks, missing_required=missing, has_characteristics=bool(position.properties)), issues, corrections


def explain(prop: Property, fact: Fact, reason: str) -> str:
    labels = {
        "invalid_value": "значение не входит в разрешённые варианты",
        "invalid_number": "ожидается число, сравнение или поддерживаемый интервал",
        "invalid_type": "ожидается значение да/нет",
        "unit_mismatch": "единица имеет другую размерность",
        "out_of_range": "значение не удовлетворяет допустимым границам",
        "unit_missing": "единица не указана, её нельзя вывести из справочника",
        "unit_unknown": "единица не распознана",
        "unsupported_constraint": "ограничение не подтверждено или не поддерживается",
    }
    constraints = []
    for item in prop.allowed:
        if item.kind in ("enum", "number"):
            constraints.append(f"{item.value} {item.unit or ''}".strip())
        else:
            left = f"{'≥' if item.lower_inclusive else '>'} {item.lower}" if item.lower is not None else ""
            right = f"{'≤' if item.upper_inclusive else '<'} {item.upper}" if item.upper is not None else ""
            constraints.append(f"{' и '.join(part for part in (left, right) if part)} {item.unit or ''}".strip())
    return f"{prop.name}: «{fact.value} {fact.unit or ''}» — {labels.get(reason, reason)}. Допустимо: {' или '.join(constraints)}."


def rule_signature(position: Position):
    """Only verified supported rules can be declared value-indistinguishable."""
    if not position.properties or any(not prop.verified or prop.required is None or not prop.allowed or
                                      prop.value_type == "unsupported" or not prop.semantic_key for prop in position.properties):
        return None
    return tuple(sorted((prop.semantic_key, prop.subject, prop.value_type, prop.required,
                         tuple(sorted((item.kind, item.value or "", item.lower or "", item.upper or "",
                                       item.lower_inclusive, item.upper_inclusive, item.unit or "") for item in prop.allowed)))
                        for prop in position.properties))
