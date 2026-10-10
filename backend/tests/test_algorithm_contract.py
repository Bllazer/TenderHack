"""C/D contract regressions on isolated synthetic constraints, without a model."""
import pytest

from algorithms.AlgorithmC import Units, check_fact, run_c
from algorithms.AlgorithmD import answered_from_facts, next_question
from algorithms.contract import Fact, Unit
from algorithms.ktru_index import Alternative, Characteristic, KtruIndex, Position


def characteristic(*alternatives, required=True, kind="2", code="size"):
    return Characteristic(code, "Size", "2", kind, required, None, list(alternatives))


def interval(low, high, lc=True, hc=True):
    return Alternative("range", lo=low, hi=high, lo_strict=not lc, hi_strict=not hc,
                       unit_code="003", unit_text="mm")


def position(code="one", char=None, blocked=False):
    return Position(code, code, False, "ACTIVE", bool(char), blocked, [], None, None,
                    None, None, [char] if char else [])


def fact(kind, value, **kwargs):
    return Fact(kind=kind, value=value, name="Size", def_id="size", unit=Unit(text="mm"),
                fact_id="fact", **kwargs)


def evaluate(char, facts, other=None):
    positions = [position(char=char)] + ([other] if other else [])
    index = KtruIndex({p.code: p for p in positions}, {}, [], "test")
    return run_c(positions, facts, index, Units(), type_verified=True, max_candidates=0)


CATALOG_RANGES = [(0, 10, True, True), (0, 10, False, True),
                  (0, 10, True, False), (0, 10, False, False),
                  (None, 10, True, True), (0, None, True, True)]
INPUT_RANGES = [(-1, 5, True, True), (0, 10, True, True), (0, 10, False, False),
                (0, 10, False, True), (0, 10, True, False), (1, 9, True, True),
                (10, 10, True, True), (10, 10, False, True), (10, 10, True, False),
                (10, 10, False, False), (11, 9, True, True), (None, 9, True, True),
                (1, None, True, True), (None, None, True, True), (-10, -1, True, True)]


@pytest.mark.parametrize("catalogue", CATALOG_RANGES)
@pytest.mark.parametrize("given", INPUT_RANGES)
def test_90_interval_containment_cases(catalogue, given):
    lo, hi, lc, hc = given
    alo, ahi, alc, ahc = catalogue
    valid = (lo is not None or hi is not None) and (
        lo is None or hi is None or lo < hi or (lo == hi and lc and hc))
    included = valid and (
        alo is None or (lo is not None and (lo > alo or (lo == alo and (alc or not lc))))) and (
        ahi is None or (hi is not None and (hi < ahi or (hi == ahi and (ahc or not hc)))))
    result, _ = check_fact(Units(), characteristic(interval(*catalogue)), fact(
        "range", {"min": lo, "max": hi, "min_inclusive": lc, "max_inclusive": hc}))
    assert (result == "compatible") is bool(included)
    if not valid:
        assert result == "unknown"


@pytest.mark.parametrize("lc,hc", [(False, True), (True, False), (False, False)])
def test_empty_catalog_interval_blocks_whole_rule(lc, hc):
    result = evaluate(characteristic(interval(750, 750, lc, hc), interval(0, 1000)), [fact("number", 750)])
    assert result.candidates[0].verdict == "unknown"
    assert not result.flags.value_verified
    assert result.selected_ktru_code is None


def test_non_point_range_does_not_prove_single_allowed_number():
    char = characteristic(Alternative("number", number=750, unit_code="003", unit_text="mm"))
    assert check_fact(Units(), char, fact("range", {"min": 700, "max": 800}))[0] == "incompatible"


@pytest.mark.parametrize("unit", [None, Unit(text="bogus"), Unit(text="dB"), Unit(text="kelvin")])
def test_unknown_or_missing_unit_never_verifies(unit):
    f = fact("number", 5)
    f.unit = unit
    assert evaluate(characteristic(interval(0, 10)), [f]).candidates[0].verdict == "unknown"


def test_or_alternatives_keep_individual_units():
    char = characteristic(Alternative("number", number=5, unit_text="mm", unit_code="003"),
                          Alternative("number", number=5, unit_text="cm", unit_code="004"))
    f = fact("number", 50)
    assert check_fact(Units(), char, f)[0] == "compatible"


@pytest.mark.parametrize("f", [fact("number", 5, polarity="uncertain"),
                              fact("number", 5, extraction_confidence=0.2)])
def test_uncertain_fact_does_not_satisfy_required_or_close_question(f):
    char = characteristic(interval(0, 10))
    result = evaluate(char, [f])
    assert result.candidates[0].verdict == "unknown"
    assert result.candidates[0].missing_required == ["Size"]
    assert not answered_from_facts([f])
    assert not result.flags.value_verified


def test_blocked_competitor_kept_unknown_not_unique():
    result = evaluate(characteristic(interval(0, 10)), [fact("number", 5)],
                      position("blocked", characteristic(interval(0, 10)), blocked=True))
    assert len(result.candidates) == 2
    assert not result.flags.ktru_unique
    assert result.selected_ktru_code is None


def test_single_candidate_still_asks_required_field():
    p = position(char=characteristic(interval(0, 10)))
    q = next_question([p], set(), KtruIndex({p.code: p}, {}, []), type_verified=True)
    assert q.reason == "fills_required"
    assert q.stop_reason is None


def test_question_unites_all_candidate_options():
    one = position("one", characteristic(Alternative("text", text="red")))
    two = position("two", characteristic(Alternative("text", text="blue")))
    one.characteristics[0].type = two.characteristics[0].type = "1"
    q = next_question([one, two], set(), KtruIndex({}, {}, []), type_verified=True)
    assert {c["value"] for c in q.choices} == {"red", "blue"}


def test_multiple_choice_checks_each_selected_value():
    char = characteristic(Alternative("text", text="red"), Alternative("text", text="blue"), kind="3")
    assert check_fact(Units(), char, fact("text_set", ["red", "blue"]))[0] == "compatible"
    assert check_fact(Units(), char, fact("text_set", ["red", "red"]))[0] == "compatible"
    assert check_fact(Units(), char, fact("text_set", ["red", "invalid"]))[0] == "incompatible"


def test_bare_number_requires_explicit_semantics_and_provenance():
    char = characteristic(Alternative("number", number=2))
    given = Fact(kind="number", value=2, name="Size", def_id="size", fact_id="n")
    assert check_fact(Units(), char, given)[1]["reason"] == "numeric_semantics_unknown"
    char.numeric_semantics = "count"
    assert check_fact(Units(), char, given)[0] == "unknown"
    char.numeric_semantics_evidence = "fixture reviewed dictionary"
    assert check_fact(Units(), char, given)[0] == "compatible"
    given.unit = Unit(text="cm")
    assert check_fact(Units(), char, given)[0] == "incompatible"


def test_comparison_is_not_an_exact_number():
    assert check_fact(Units(), characteristic(interval(0, 10)), fact("number", 5, operator=">="))[0] == "incompatible"


def test_adjacent_or_ranges_cover_input_without_erasing_open_hole():
    f = fact("range", {"min": 1, "max": 4})
    assert check_fact(Units(), characteristic(interval(0, 2), interval(2, 5)), f)[0] == "compatible"
    assert check_fact(Units(), characteristic(interval(0, 2, True, False), interval(2, 5, False, True)), f)[0] == "incompatible"


def test_equivalent_units_do_not_conflict():
    first, second = fact("number", 5), fact("number", .5)
    second.unit = Unit(text="cm")
    assert "conflicting_facts" not in evaluate(characteristic(interval(0, 10)), [first, second]).flags.blocking


def test_decimal_conversion_preserves_closed_and_open_boundaries():
    f = fact("number", .7)
    f.unit = Unit(text="m")
    assert check_fact(Units(), characteristic(interval(700, 800)), f)[0] == "compatible"
    assert check_fact(Units(), characteristic(interval(700, 800, False, True)), f)[0] == "incompatible"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_numbers_do_not_verify(value):
    assert check_fact(Units(), characteristic(interval(0, 10)), fact("number", value))[0] == "unknown"
