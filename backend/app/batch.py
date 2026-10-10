"""Сводка по результату анализа для пакетной обработки CSV.

Пакет — это те же одиночные анализы, поэтому наружу нужен не полный ответ
(двадцать полных карточек в одном запросе), а компактная строка отчёта:
статус, выбранный код, число кандидатов, следующий вопрос и одна причина
незавершения в терминах пайплайна.
"""
from __future__ import annotations

from typing import Optional

#: причины незавершения из плана: type_unknown, mapping_missing, missing_required,
#: conflicting_facts, value_indistinguishable, catalog_link_missing,
#: unsupported_constraint + служебные confirm_code_required / no_compatible_candidate
MAPPING_REASONS = {"unit_missing", "unit_unknown", "unit_mismatch", "numeric_semantics_unknown",
                   "mapping_ambiguous", "unresolved_property_correction", "unresolved_value_correction",
                   "unmapped_measurement"}
UNSUPPORTED_REASONS = {"catalog_constraints_unavailable", "unsupported_constraint"}


def non_completion_reason(result) -> Optional[str]:
    """Одна причина, по которой подбор не завершён. None для resolved."""
    if result.status == "resolved":
        return None
    issues = {issue.code for issue in result.issues}
    question = result.next_question
    if "conflicting_facts" in issues:
        return "conflicting_facts"
    if question is not None and question.kind == "conflict":
        return "conflicting_facts"
    if result.verification.value_indistinguishable:
        return "value_indistinguishable"
    if question is not None and question.kind == "code":
        return "confirm_code_required"
    if not result.scope.category_id:
        return "type_unknown"
    if result.status == "needs_data":
        return "unsupported_constraint" if issues & UNSUPPORTED_REASONS else "catalog_link_missing"
    blocking = [reason for candidate in result.candidates for reason in candidate.blocking_reasons
                if reason.blocks_resolution]
    if blocking or issues & UNSUPPORTED_REASONS:
        return "unsupported_constraint"
    if not result.candidates and result.excluded:
        return "no_compatible_candidate"
    if not result.candidates:
        return "catalog_link_missing"
    if issues & MAPPING_REASONS:
        return "mapping_missing"
    if any(candidate.missing_required for candidate in result.candidates):
        return "missing_required"
    if any(candidate.outcome == "unknown" for candidate in result.candidates):
        return "verification_incomplete"
    return "confirm_code_required" if len(result.candidates) == 1 else "value_indistinguishable"


def summarize(result) -> dict:
    """Компактная строка отчёта по одному анализу (без полных проверок и фактов)."""
    question = result.next_question
    return {
        "status": result.status,
        "selected_ktru_code": result.selected_ktru_code,
        "candidate_count": result.candidate_count,
        "excluded_count": result.excluded_count,
        "outcome": result.candidates[0].outcome if len(result.candidates) == 1 else None,
        "top_candidates": [candidate.ktru_code for candidate in result.candidates[:3]],
        "category_id": result.scope.category_id,
        "scope_complete": result.scope.complete,
        "question_kind": question.kind if question else None,
        "question_title": question.title if question else None,
        "question_property": question.property_name if question else None,
        "issue_codes": sorted({issue.code for issue in result.issues}),
        "reason": non_completion_reason(result),
        "verification": result.verification.model_dump(),
    }


def batch_counts(rows: list) -> dict:
    """Сводка по всему файлу: сколько resolved, какие причины преобладают."""
    reasons: dict = {}
    statuses: dict = {}
    for row in rows:
        statuses[row["status"]] = statuses.get(row["status"], 0) + 1
        reason = row.get("reason")
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1
    return {"rows": len(rows), "statuses": statuses, "reasons": reasons,
            "resolved": statuses.get("resolved", 0)}
