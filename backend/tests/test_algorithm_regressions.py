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


# ---------------------------------------- OR-альтернативы в разных единицах
def given(bounds, unit="mm"):
    return Fact(kind="range", value=bounds, name="Size", def_id="size",
                unit=Unit(text=unit) if unit else None, fact_id="fact")


def span(low, high, lc=True, hc=True):
    return {"min": low, "max": high, "min_inclusive": lc, "max_inclusive": hc}


def test_or_alternatives_in_different_units_are_not_merged_raw_temperature():
    """[0;10] °C OR [300;310] K: Кельвин приводится к Цельсию ДО объединения.

    Без сдвига температуры 300…310 K складывались бы с 0…10 °C как сырые числа, и
    требование [300;305] °C «подтверждалось» бы чужими значениями.
    """
    char = characteristic(interval(0, 10, unit_code=None, unit_text="°C"),
                          interval(300, 310, unit_code=None, unit_text="K"))
    # 300…310 K — это 26,85…36,85 °C, поэтому требование на 300 °C не подтверждается
    assert check_fact(Units(), char, given(span(300, 305), "°C"))[0] == "incompatible"
    # значение внутри кельвиновой ветки подтверждается именно после сдвига
    assert check_fact(Units(), char, given(span(27, 36), "°C"))[0] == "compatible"
    # разрыв между ветками (10…26,85 °C) не покрыт
    assert check_fact(Units(), char, given(span(11, 26), "°C"))[0] == "incompatible"
    # собственная ветка продолжает работать
    assert check_fact(Units(), char, given(span(5, 8), "°C"))[0] == "compatible"


def test_or_units_temperature_refusal_selects_nothing():
    """Отказ по такому OR не даёт выбрать код."""
    char = characteristic(interval(0, 10, unit_code=None, unit_text="°C"),
                          interval(300, 310, unit_code=None, unit_text="K"))
    result = run(position("one", char), facts=[given(span(300, 305), "°C")])
    assert result.candidates[0].verdict == "incompatible"
    assert result.selected_ktru_code is None


def test_or_alternatives_cover_together_after_canonicalisation():
    """[0;1] см OR [10;30] мм покрывают [5;25] мм только вместе.

    Каждая ветка по отдельности требование не покрывает: 0…1 см — это 0…10 мм,
    ровно до начала второй ветки. Проверять группы порознь нельзя.
    """
    char = characteristic(interval(0, 1, unit_code=None, unit_text="см"),
                          interval(10, 30, unit_code=None, unit_text="мм"))
    assert check_fact(Units(), char, given(span(5, 25), "мм"))[0] == "compatible"
    assert check_fact(Units(), char, given(span(0, 30), "мм"))[0] == "compatible"
    assert check_fact(Units(), char, given(span(5, 25, False, False), "мм"))[0] == "compatible"
    # на конце объединения подтверждения нет
    assert check_fact(Units(), char, given(span(25, 35), "мм"))[0] == "incompatible"


def test_strict_boundary_at_the_junction_is_preserved():
    """[0;1) см OR (10;30] мм: точка 10 мм исключена обеими ветками — дыра не зарастает."""
    char = characteristic(interval(0, 1, True, False, unit_code=None, unit_text="см"),
                          interval(10, 30, False, True, unit_code=None, unit_text="мм"))
    assert check_fact(Units(), char, given(span(5, 25), "мм"))[0] == "incompatible"
    assert check_fact(Units(), char, given(span(10, 25), "мм"))[0] == "incompatible"
    assert check_fact(Units(), char, given(span(11, 25), "мм"))[0] == "compatible"


@pytest.mark.parametrize("second,expected", [
    (Alternative("number", number=5), "unknown"),                    # безразмерная рядом с размерной
    (interval(10, 30, unit_code=None, unit_text="Вт"), "unknown"),   # другая размерность
])
def test_incomparable_or_alternatives_are_not_merged(second, expected):
    char = characteristic(interval(0, 1, unit_code=None, unit_text="см"), second)
    assert check_fact(Units(), char, given(span(5, 6), "мм"))[0] == expected
