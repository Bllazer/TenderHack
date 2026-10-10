"""Three-valued constraint checking. Unknown is never success."""
import re
import json
from dataclasses import dataclass
from decimal import Decimal

from app.catalog import AllowedValue, Position, Property
from app.mapping import map_facts
from app.schemas import Candidate, Check, Correction, ElementCheck, Fact, Issue
from app.selected_values import selected_values
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

    @property
    def empty(self) -> bool:
        return self.lower > self.upper or (self.lower == self.upper and
                                           not (self.lower_inclusive and self.upper_inclusive))

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
    return None if result.empty else result


def allowed_interval(item: AllowedValue) -> Interval:
    if item.kind == "number":
        value = Decimal(item.value)
        return Interval(value, value)
    return Interval(Decimal(item.lower) if item.lower is not None else -INF,
                    Decimal(item.upper) if item.upper is not None else INF,
                    item.lower_inclusive, item.upper_inclusive)


def subset(query: Interval, allowed: list[Interval]) -> bool:
    if query.empty or any(interval.empty for interval in allowed):
        return False
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


def signature(fact: Fact, prop: Property, unit_resolver=None):
    # Canonical comparison is independent of the wire representation. Keep the
    # original fact (including array order, repetitions and provenance) intact.
    values = set()
    unit = unit_resolver(prop, fact.unit) if unit_resolver else get_unit(fact.unit)
    for value in selected_values(prop, fact):
        parsed = number(value) if prop.value_type == "number" else None
        if parsed:
            dimension = (unit.dimension if unit else "count"
                         if not fact.unit and prop.numeric_semantics == "count"
                         and prop.numeric_semantics_evidence and all(not item.unit for item in prop.allowed)
                         else canon(fact.unit or ""))
            values.add((parsed.scaled(unit.factor) if unit else parsed,
                        dimension))
        else:
            values.add((canon(value), canon(fact.unit or "")))
    return frozenset(values)


def check_fact(prop: Property, fact: Fact, unit_resolver=None):
    """outcome, reason, value_codes, optional safe normalization."""
    if fact.unknown:
        return "unknown", "unknown_fact", [], None
    if not prop.verified or prop.unsupported_reasons or not prop.evidence or prop.required is None or prop.value_type == "unsupported" or prop.kind not in ("1", "2", "3"):
        return "unknown", "unsupported_constraint", [], None
    if not prop.allowed:
        return "unknown", "no_supported_constraints", [], None
    values = selected_values(prop, fact)
    if fact.values is not None or len(values) > 1:
        if prop.kind != "3" and len(signature(fact, prop, unit_resolver)) > 1:
            return "incompatible", "multiple_values_not_allowed", [], None
        results = [check_fact(prop, fact.model_copy(update={"value": v, "values": None}), unit_resolver) for v in values]
        outcome = "incompatible" if any(r[0] == "incompatible" for r in results) else "unknown" if any(r[0] == "unknown" for r in results) else "compatible"
        reason = next((r[1] for r in results if r[0] == outcome), "empty_selection")
        return outcome, reason, sorted({c for r in results for c in r[2]}), None
    if prop.value_type in ("text", "boolean"):
        if fact.unit and all(not item.unit for item in prop.allowed):
            return "unknown", "unit_mismatch", [], None
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
    bare = any(unit is None for unit in units)
    if bare and (prop.numeric_semantics not in ("count", "designation") or not prop.numeric_semantics_evidence):
        return "unknown", "numeric_semantics_unknown", [], None
    dimensions = {unit.dimension if unit else "count" if prop.numeric_semantics == "count" else "dimensionless" for unit in units}
    if len(dimensions) != 1:
        return "unknown", "unsupported_constraint", [], None
    supplied = unit_resolver(prop, fact.unit) if unit_resolver else get_unit(fact.unit)
    expected_dimension = next(iter(dimensions))
    if not bare and expected_dimension != "dimensionless" and not fact.unit:
        return "unknown", "unit_missing", [], None
    if fact.unit and supplied is None:
        return "unknown", "unit_unknown", [], None
    input_dimension = supplied.dimension if supplied else expected_dimension if bare else "dimensionless"
    if input_dimension != expected_dimension:
        return "incompatible", "unit_mismatch", [], None
    physical = parsed.scaled(supplied.factor if supplied else Decimal(1))
    if prop.numeric_semantics == "count":
        if not prop.numeric_semantics_evidence:
            return "unknown", "numeric_semantics_unknown", [], None
        if any(bound.is_finite() and bound != bound.to_integral_value()
               for bound in (physical.lower, physical.upper)):
            return "incompatible", "invalid_count", [], None
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


def evaluate(position: Position, facts: list[Fact], unit_resolver=None):
    groups, issues, corrections = map_facts(position, facts)
    checks: list[Check] = []
    missing: list[str] = []
    has_checked_value = False
    for prop in position.properties:
        found = groups[prop.id]
        known = [fact for fact in found if not fact.unknown]
        if prop.required is True and not known:
            missing.append(prop.name)
        if prop.kind != "3" and len({signature(fact, prop, unit_resolver) for fact in known}) > 1:
            issues.append(Issue(code="conflicting_facts", property_id=prop.id, candidate_code=position.code,
                                fact_id=known[0].id, rule_source=prop.evidence,
                                explanation=f"Для «{prop.name}» указаны противоречащие значения: " + ", ".join(fact.value for fact in known),
                                suggested_action="Исправьте конкретный факт или исходное описание", confirmation_required=True))
            checks.append(Check(property_id=prop.id, name=prop.name, outcome="unknown", reason="conflicting_facts",
                                fact_ids=[fact.id for fact in found]))
            continue
        if not found:
            reason = "missing_required" if prop.required else "not_provided"
            if prop.required is not False and (not prop.verified or prop.required is None or not prop.allowed or prop.value_type == "unsupported" or prop.kind not in ("1", "2", "3")):
                reason = "unsupported_constraint"
            checks.append(Check(property_id=prop.id, name=prop.name, outcome="unknown", reason=reason))
            continue
        results = [check_fact(prop, fact, unit_resolver) for fact in found]
        outcome = ("incompatible" if any(row[0] == "incompatible" for row in results) else
                   "unknown" if any(row[0] == "unknown" for row in results) else "compatible")
        selected = next((row for row in results if row[0] == outcome), results[0])
        elements = []
        for fact in found:
            values = selected_values(prop, fact)
            if fact.values is not None or len(values) > 1:
                for value in values:
                    row = check_fact(prop, fact.model_copy(update={"value": value, "values": None}), unit_resolver)
                    elements.append(ElementCheck(value=value, outcome=row[0], reason=row[1], value_codes=row[2]))
        checks.append(Check(property_id=prop.id, name=prop.name, outcome=outcome, reason=selected[1], elements=elements,
                            fact_ids=[fact.id for fact in found], value_codes=sorted({code for row in results for code in row[2]})))
        has_checked_value |= outcome == "compatible"
        for fact, row in zip(found, results):
            if row[3]:
                normalized, unit = row[3]
                fact.normalized_value, fact.normalized_unit = normalized, unit
                if normalized != fact.value or (unit and unit != fact.unit):
                    corrections.append(Correction(fact_id=fact.id, kind="auto_normalization", original=f"{fact.value} {fact.unit or ''}".strip(),
                                                  normalized=f"{normalized} {unit or ''}".strip(), explanation="Безопасное преобразование в совместимой размерности"))
            if row[1] in ("invalid_value", "invalid_number", "invalid_type", "unit_mismatch", "out_of_range", "unit_missing", "unit_unknown", "unsupported_constraint", "numeric_semantics_unknown", "invalid_count", "multiple_values_not_allowed"):
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
    if position.source_issues or position.resolve_blocked is True:
        issues.append(Issue(code="source_issue" if position.source_issues else "resolve_blocked", candidate_code=position.code,
                            rule_source=position.source_path, explanation="Позиция заблокирована источником для завершения подбора",
                            suggested_action="Уточните ограничения исходного КТРУ"))
        checks.append(Check(property_id="", name="Источник", outcome="unknown", reason="source_issue" if position.source_issues else "resolve_blocked"))
        if outcome != "incompatible":
            outcome = "unknown"
    return Candidate(ktru_code=position.code, name=position.name, okpd2=position.okpd2, outcome=outcome,
                     checks=checks, missing_required=missing, has_characteristics=bool(position.properties)), issues, corrections


def explain(prop: Property, fact: Fact, reason: str) -> str:
    labels = {
        "invalid_value": "значение не входит в разрешённые варианты",
        "invalid_number": "ожидается число, сравнение или поддерживаемый интервал",
        "invalid_type": "ожидается значение да/нет",
        "invalid_count": "счётное значение должно быть целым",
        "numeric_semantics_unknown": "смысл числа без исходной единицы не подтверждён",
        "multiple_values_not_allowed": "источник допускает выбор только одного значения",
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
    displayed = json.dumps(fact.values, ensure_ascii=False) if fact.values is not None else fact.value
    return f"{prop.name}: «{displayed} {fact.unit or ''}» — {labels.get(reason, reason)}. Допустимо: {' или '.join(constraints)}."


def rule_signature(position: Position):
    """Only verified supported rules can be declared value-indistinguishable."""
    if not position.properties or any(not prop.verified or prop.required is None or not prop.allowed or
                                      prop.value_type == "unsupported" or not prop.semantic_key for prop in position.properties):
        return None
    return tuple(sorted((prop.semantic_key, prop.subject, prop.value_type, prop.required,
                         tuple(sorted((item.kind, item.value or "", item.lower or "", item.upper or "",
                                       item.lower_inclusive, item.upper_inclusive, item.unit or "") for item in prop.allowed)))
                        for prop in position.properties))
