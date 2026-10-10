"""Регрессии AlgorithmC после merge f47cd06 (53f365e + e6a0375).

Каждый тест воспроизводит поведение, которое сломалось при слиянии, и которое
нельзя терять: выбор шаблона, подтверждение значения мягким фактом, строгость
границ и односторонние интервалы.
"""
from __future__ import annotations

import pytest

from algorithms.AlgorithmC import Units, check_fact, run_c
from algorithms.contract import Fact, Unit
from algorithms.ktru_index import Alternative, Characteristic, KtruIndex, Position


def characteristic(*alternatives, required=True, kind="2", code="size"):
    return Characteristic(code, "Size", "2", kind, required, None, list(alternatives))


def interval(low, high, lc=True, hc=True, unit_code="003", unit_text="mm"):
    return Alternative("range", lo=low, hi=high, lo_strict=not lc, hi_strict=not hc,
                       unit_code=unit_code, unit_text=unit_text)


def position(code="one", char=None, template=False, blocked=False):
    return Position(code, code, template, "ACTIVE", bool(char), blocked, [], None, None,
                    None, None, [char] if char else [])


def fact(kind, value, **kwargs):
    return Fact(kind=kind, value=value, name="Size", def_id="size", unit=Unit(text="mm"),
                fact_id="fact", **kwargs)


def run(*positions, facts, units=None):
    return run_c(list(positions), facts, KtruIndex({p.code: p for p in positions}, {}, [], "test"),
                 units or Units(), type_verified=True, max_candidates=0)


CHAR = characteristic(interval(0, 10))


# --------------------------------------------------------------- шаблоны
def test_unblocked_template_is_never_a_candidate_or_a_selection():
    """Шаблон исключается независимо от resolve_blocked.

    Раньше в `usable` попадали все позиции, кроме шаблонов, но фильтр стоял по
    resolve_blocked — и шаблон без этого флага становился кандидатом, а если он в
    пуле один, то и итоговым кодом.
    """
    good = position("good", CHAR)
    template = position("tmpl", CHAR, template=True, blocked=False)
    result = run(good, template, facts=[fact("number", 5)])
    assert [c.code for c in result.candidates] == ["good"]
    assert result.selected_ktru_code == "good"
    assert result.counts["before"] == 1


def test_blocked_template_is_excluded_too():
    good = position("good", CHAR)
    template = position("tmpl", CHAR, template=True, blocked=True)
    result = run(good, template, facts=[fact("number", 5)])
    assert [c.code for c in result.candidates] == ["good"]
    assert result.selected_ktru_code == "good"
    assert result.counts["blocked_excluded"] == 0        # шаблон — не «конечная позиция»


def test_template_alone_never_selects_a_code():
    template = position("tmpl", CHAR, template=True, blocked=False)
    result = run(template, facts=[fact("number", 5)])
    assert result.candidates == []
    assert result.selected_ktru_code is None


def test_blocked_final_position_stays_an_unknown_competitor():
    """Заблокированная КОНЕЧНАЯ позиция не доказана несовместимой: она конкурирует."""
    good = position("good", CHAR)
    blocked = position("blocked", CHAR, template=False, blocked=True)
    result = run(good, blocked, facts=[fact("number", 5)])
    assert [(c.code, c.verdict) for c in result.candidates] == [("blocked", "unknown"),
                                                                ("good", "compatible")]
    assert not result.flags.ktru_unique
    assert result.selected_ktru_code is None


# ----------------------------------------------- мягкие факты не подтверждают
@pytest.mark.parametrize("fact_value", [
    fact("number", 5, extraction_confidence=0.2),
    fact("number", 5, polarity="uncertain"),
])
def test_unverified_fact_does_not_confirm_value_or_close_required_field(fact_value):
    """Мягкий или неопределённый факт не подтверждает значение и не закрывает поле."""
    result = run(position("good", CHAR), facts=[fact_value])
    candidate = result.candidates[0]
    assert candidate.verdict == "unknown"
    assert candidate.missing_required == ["Size"]
    assert [u.reason for u in candidate.unresolved] == ["uncertain_or_low_confidence_fact"] \
        or [u.reason for u in candidate.unresolved] == ["uncertain_assertion"]
    assert not result.flags.value_verified
    assert result.selected_ktru_code is None


def test_low_confidence_fact_does_not_become_hard_filter():
    """Мягкий факт проверяется, но никого не исключает."""
    result = run(position("good", CHAR), facts=[fact("number", 5, extraction_confidence=0.2)])
    assert result.candidates[0].exclusions == []
    assert list(result.survivors) == ["good"]


# -------------------------------------------------- строгость и односторонность
@pytest.mark.parametrize("catalogue,given,expected", [
    # справочник замкнут, требование открыто — подмножество
    (interval(0, 10), {"min": 0, "max": 10, "min_inclusive": False, "max_inclusive": False},
     "compatible"),
    # справочник строгий, требование замкнуто — включает исключённую точку
    (interval(0, 10, False, True), {"min": 0, "max": 10, "min_inclusive": True, "max_inclusive": True},
     "incompatible"),
    # оба строгие на одной границе — подмножество
    (interval(0, 10, False, True), {"min": 0, "max": 10, "min_inclusive": False, "max_inclusive": True},
     "compatible"),
    # одностороннее требование не подтверждается ограниченным справочником
    (interval(0, 10), {"min": 5, "max": None, "min_inclusive": True, "max_inclusive": True},
     "incompatible"),
    # односторонний справочник покрывает одностороннее требование
    (interval(0, None), {"min": 5, "max": None, "min_inclusive": True, "max_inclusive": True},
     "compatible"),
])
def test_inclusive_flags_and_one_sided_intervals(catalogue, given, expected):
    assert check_fact(Units(), characteristic(catalogue), fact("range", given))[0] == expected


def test_user_range_including_strict_catalogue_boundary_is_not_confirmed():
    """Точка на строгой границе справочника не подтверждает код."""
    strict = characteristic(interval(700, 800, False, True))     # (700;800]
    assert check_fact(Units(), strict, fact("range", {"min": 700, "max": 800,
                                                      "min_inclusive": True,
                                                      "max_inclusive": True}))[0] == "incompatible"
    assert check_fact(Units(), strict, fact("range", {"min": 700, "max": 800,
                                                      "min_inclusive": False,
                                                      "max_inclusive": True}))[0] == "compatible"


def test_empty_catalogue_interval_blocks_the_whole_rule():
    """Пустая альтернатива в OR не «спасается» соседней исправной."""
    broken = characteristic(interval(750, 750, False, True), interval(0, 1000))
    assert check_fact(Units(), broken, fact("number", 750))[0] == "unknown"
