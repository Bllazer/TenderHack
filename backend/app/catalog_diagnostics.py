"""Read-only diagnostics of immutable constraints, independent of user facts."""
from app.schemas import BlockingReason


def position_blockers(position):
    result = []
    if not position.properties:
        result.append(BlockingReason(code="no_characteristics", explanation="Нет проверяемых характеристик позиции"))
    for prop in position.properties:
        reasons = list(prop.unsupported_reasons)
        if not prop.verified and not reasons:
            reasons.append("unverified_constraint")
        if not prop.allowed:
            reasons.append("no_supported_constraints")
        if prop.required is None:
            reasons.append("requiredness_unknown")
        if prop.kind not in ("1", "2") and not reasons:
            reasons.append("unsupported_kind")
        if reasons:
            result.append(BlockingReason(code="unsupported_constraint", property_id=prop.id,
                property_name=prop.name, kind=prop.kind, units=[item.raw_unit or item.unit for item in prop.allowed],
                unsupported_reasons=sorted(set(reasons)), rule_source=prop.evidence,
                explanation=f"«{prop.name}» (kind={prop.kind}): " + ", ".join(sorted(set(reasons)))))
    if position.source_issues:
        result.append(BlockingReason(code="source_issue", source_issues=list(position.source_issues),
            rule_source=position.source_path, explanation="В поставке есть блокирующие проблемы источника"))
    if position.resolve_blocked is True:
        result.append(BlockingReason(code="resolve_blocked", resolve_blocked_reason=list(position.resolve_blocked_reason),
            rule_source=position.source_path, explanation="Источник запретил завершение: " + ", ".join(position.resolve_blocked_reason)))
    return result
