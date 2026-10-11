"""Ограничения значений: диапазон в вопросе из найденных позиций и отсечение вбросов.

Проверяются правила, а не константы: границы берутся из справочника позиций, поэтому
изменение интервала в позиции обязано изменить и подсказку, и решение о вбросе.
"""
from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "tests"))

from app.analysis import (implausible_measurements, merge_answer_issues,  # noqa: E402
                         out_of_scale_values)
from app.catalog import (AllowedValue, Catalog, Category, Position, Property,  # noqa: E402
                         load_snapshot, snapshot)
from app.questions import allowed_ranges, property_question, ranges_hint  # noqa: E402
from app.schemas import Fact, Issue  # noqa: E402
from app.units import get_unit  # noqa: E402

INDEX_DIR = Path(os.environ.get("TEST_INDEX_DIR", "resources/catalog-build"))


def numeric_property(lower: str, upper: str, unit: str = "мм", name: str = "Внутренний диаметр") -> Property:
    return Property(id="diameter", name=name, value_type="number", required=True, verified=True,
                    evidence="test", kind="1",
                    allowed=(AllowedValue(kind="interval", lower=lower, upper=upper, unit=unit,
                                          raw_unit=unit, source="test"),))


def enum_property() -> Property:
    return Property(id="type", name="Тип", value_type="text", required=True, verified=True,
                    evidence="test",
                    allowed=(AllowedValue(kind="enum", value="Кулачок", source="test"),
                             AllowedValue(kind="enum", value="Плотничный", source="test")))


# ------------------------------------------------------- диапазон в вопросе
@pytest.mark.parametrize("lower,upper,hint", [
    ("1", "10", "Допустимо: от 1 до 10 мм"),
    ("2", "400", "Допустимо: от 2 до 400 мм"),
    ("5", "60", "Допустимо: от 5 до 60 мм"),
])
def test_question_range_comes_from_the_positions(lower, upper, hint):
    """Подсказка о диапазоне — из интервала характеристики, а не из констант кода."""
    question = property_question("cat", numeric_property(lower, upper), "Различает позиции")
    assert [(item.lower, item.upper, item.unit) for item in question.ranges] == [(float(lower), float(upper), "мм")]
    assert question.hint == hint
    assert question.unit == "мм"


def test_question_range_changes_with_the_interval():
    """Меняем интервал в позиции — меняется подсказка: значение не зашито."""
    narrow = property_question("cat", numeric_property("1", "2"), "Различает позиции")
    wide = property_question("cat", numeric_property("100", "200"), "Различает позиции")
    assert narrow.hint != wide.hint
    assert "1" in narrow.hint and "100" in wide.hint


def test_question_without_bounds_has_no_hint():
    """Перечисление не ограничивается диапазоном: подсказки быть не должно."""
    question = property_question("cat", enum_property(), "Различает позиции")
    assert question.ranges == []
    assert question.hint is None


def test_ranges_merge_overlapping_intervals():
    """Объединение интервалов: пересекающиеся сливаются, разные единицы не смешиваются."""
    prop = Property(id="d", name="Диаметр", value_type="number", required=True, verified=True,
                    evidence="test", kind="1",
                    allowed=(AllowedValue(kind="interval", lower="1", upper="5", unit="мм", source="test"),
                             AllowedValue(kind="interval", lower="3", upper="9", unit="мм", source="test"),
                             AllowedValue(kind="interval", lower="0.5", upper="2", unit="см", source="test")))
    ranges = allowed_ranges(prop)
    millimetres = sorted((item.lower, item.upper) for item in ranges if item.unit == "мм")
    centimetres = [(item.lower, item.upper) for item in ranges if item.unit == "см"]
    assert millimetres == [(1.0, 9.0)]
    assert centimetres == [(0.5, 2.0)]
    assert ranges_hint(ranges) == "Допустимо: от 1 до 9 мм или от 0.5 до 2 см"


# ------------------------------------------------- замечания по одному ответу
def test_answer_issues_are_merged_into_one_with_union_of_allowed():
    """Двенадцать строк про один ответ превращаются в одну с объединением допустимых."""
    issues = [Issue(code="invalid_value", fact_id="answer:property:1", property_id="p1",
                    explanation=f"Тип: «2321122112 » — значение не входит в разрешённые варианты. Допустимо: {value}.")
              for value in ("Кулачок", "Плотничный", "Паркетный", "Кулачок")]
    merged = merge_answer_issues(issues)
    assert len(merged) == 1
    assert merged[0].explanation == ("Тип: «2321122112 » — значение не входит в разрешённые варианты. "
                                     "Допустимо: Кулачок, Плотничный, Паркетный.")


def test_issues_not_from_answers_are_untouched():
    """Замечания о позициях не склеиваются: там важна конкретная позиция."""
    issues = [Issue(code="out_of_range", candidate_code="A", explanation="Диаметр вне границ"),
              Issue(code="out_of_range", candidate_code="B", explanation="Диаметр вне границ")]
    assert len(merge_answer_issues(issues)) == 2


# ------------------------------------------------------- слова вместо сокращений
@pytest.mark.parametrize("word,name", [
    ("метров", "м"), ("метр", "м"), ("метра", "м"),
    ("миллиметров", "мм"), ("сантиметров", "см"),
    ("килограммов", "кг"), ("граммов", "г"), ("ватт", "Вт"), ("штук", "шт"),
])
def test_unit_words_are_known(word, name):
    """«метров» и прочие слова — известные единицы, иначе вброс не проверяется."""
    unit = get_unit(word)
    assert unit is not None and unit.name == name


@pytest.fixture(scope="module")
def unit_supply():
    if not (INDEX_DIR / "manifest.json").exists():
        pytest.skip(f"нет снимка: {INDEX_DIR}")
    from app.algorithm_adapter import make_units
    data = load_snapshot(INDEX_DIR)
    return data, make_units(data)


def test_supply_aliases_gain_word_forms(unit_supply):
    """Мост единиц: слова привязываются к кодам ОКЕИ поставки, не подменяя её."""
    from app.algorithm_adapter import _norm_unit
    data, units = unit_supply
    assert len(units.aliases) > len(data.catalog.unit_aliases)
    entry = units.aliases.get(_norm_unit("метров"))
    assert entry is not None and entry["okei_name"] == "Метр"
    # сокращения поставки не переопределены
    for alias in data.catalog.unit_aliases:
        assert units.aliases[_norm_unit(alias.raw)]["okei_code"] == alias.okei_code


# ------------------------------------------------- проверка вброса по позициям
def measurement_snapshot(lower: str, upper: str, name: str = "Внутренний диаметр",
                         version: str | None = None, code: str = "H1", unit: str = "мм") -> object:
    """Снимок с одной позицией и одной числовой характеристикой (для проверок границ)."""
    category = Category(id="hose", name="Шланг лабораторный", search_text="шланг лабораторный",
                        position_codes=(code,))
    position = Position(code=code, name="Шланг лабораторный", source_path="test", status="actual",
                        is_template=False,
                        properties=(numeric_property(lower, upper, unit=unit, name=name),))
    catalog = Catalog(format_version=1, catalog_version=version or f"limits-{name}-{lower}-{upper}-{unit}",
                      source_description="test", validation_report="test", synthetic=True,
                      categories=[category], positions=[position])
    return snapshot(catalog, vectors=[1.0], tokens=(("шланг", "лабораторный"),))


def scale_cases(data, name: str, values, group: str = "H1") -> dict:
    """Для каждого (значение, единица) — серьёзность замечания о масштабе или None."""
    result = {}
    for value, unit in values:
        fact = Fact(id="f", name=name, value=value, unit=unit, raw=f"{value} {unit}",
                    span=(0, 5), source="text")
        issues = out_of_scale_values(data, [fact], set(), {group})
        result[(value, unit)] = issues[0].severity if issues else None
    return result


def test_out_of_scale_value_is_rejected_when_catalogue_range_is_open():
    """«200 метров» у длины рукоятки: справочник открыт сверху, но масштаб проверяется.

    Так выглядел случай со скриншота: у «Длина рукоятки» последний интервал — «900 и более
    мм», поэтому обычная проверка границ молчала, и значение доходило до подтверждения кода.
    """
    data = measurement_snapshot("100", "900", name="Длина рукоятки")
    cases = scale_cases(data, "Длина рукоятки", [("200", "метров"), ("20000", "мм"), ("3000", "мм"),
                                                 ("500", "мм"), ("50", "мм")])
    assert cases[("200", "метров")] == "error"      # 200 000 мм против наблюдаемых 100–900 мм
    assert cases[("20000", "мм")] == "error"        # десятикратный выход
    assert cases[("3000", "мм")] == "warning"       # двукратный выход
    assert cases[("500", "мм")] is None             # внутри наблюдаемого
    assert cases[("50", "мм")] is None              # на границе допустимого (100 / 2)


def test_scale_check_follows_catalogue_data():
    """Расширили наблюдаемый диапазон — значение перестало быть подозрительным."""
    strict = measurement_snapshot("100", "900", name="Длина рукоятки")
    loose = measurement_snapshot("100", "90000", name="Длина рукоятки")
    assert scale_cases(strict, "Длина рукоятки", [("20000", "мм")])[("20000", "мм")] == "error"
    assert scale_cases(loose, "Длина рукоятки", [("20000", "мм")])[("20000", "мм")] is None


def test_scale_check_is_scoped_to_the_found_group():
    """Масштаб берётся у найденной группы КТРУ, а не по всему каталогу.

    Регрессия, найденная набором сценариев обновления: «Объем баллона» у аэрозолей
    измеряется миллилитрами, у газовых баллонов — литрами. Общие границы по названию
    объявляли нормальные 11 л выходом за масштаб, и сценарий «Газ природный» переставал
    быть ambiguous.
    """
    aerosol = measurement_snapshot("0.5", "80", name="Объем баллона", version="aerosol",
                                   code="20.42.15.110-00000001", unit="мл")
    cylinder = measurement_snapshot("5", "50", name="Объем баллона", version="cylinder",
                                    code="06.20.10.110-00000003", unit="л")
    fact = Fact(id="f", name="Объем баллона", value="11", unit="л", raw="11 л",
                span=(0, 4), source="text")
    # у аэрозолей 11 л действительно вне масштаба, у баллонов — норма
    assert out_of_scale_values(aerosol, [fact], set(), {"20.42.15.110"})
    assert out_of_scale_values(cylinder, [fact], set(), {"06.20.10.110"}) == []


def test_scale_check_normalises_units_inside_a_group():
    """Внутри группы единицы приводятся к основной: 11000 мл против 500–80000 мл молча."""
    data = measurement_snapshot("500", "80000", name="Объем баллона", unit="мл")
    assert scale_cases(data, "Объем баллона", [("11", "л"), ("500", "мл"), ("500", "л")]) == {
        ("11", "л"): None, ("500", "мл"): None, ("500", "л"): "warning"}


def test_scale_check_ignores_unknown_characteristics_and_unmapped_facts():
    """Неизвестная характеристика и факт без имени не судятся: сравнивать не с чем."""
    data = measurement_snapshot("100", "900", name="Длина рукоятки")
    unknown = Fact(id="f", name="Неведомая величина", value="200", unit="метров",
                   raw="200 метров", span=(0, 10), source="text")
    anonymous = Fact(id="f2", name="", value="200", unit="метров", raw="200 метров",
                     span=(0, 10), source="text")
    assert out_of_scale_values(data, [unknown], set(), {"H1"}) == []
    assert out_of_scale_values(data, [anonymous], set(), {"H1"}) == []


def test_scale_check_without_groups_stays_silent():
    """Без найденных групп судить не о чем: пустой набор групп — нет замечаний."""
    data = measurement_snapshot("100", "900", name="Длина рукоятки")
    fact = Fact(id="f", name="Длина рукоятки", value="200", unit="метров", raw="200 метров",
                span=(0, 10), source="text")
    assert out_of_scale_values(data, [fact], set(), set()) == []


def test_scale_check_converts_units_before_comparing():
    """Единицы приводятся к основной: 0.2 м и 0.5 м против наблюдаемых 100–900 мм — молча."""
    data = measurement_snapshot("100", "900", name="Длина рукоятки")
    cases = scale_cases(data, "Длина рукоятки", [("0.2", "метров"), ("0.5", "метров")])
    assert cases[("0.2", "метров")] is None
    assert cases[("0.5", "метров")] is None


def test_implausible_measurement_is_reported_against_found_positions():
    """«200 метров» при диапазоне позиции 1–500 мм — замечание с допустимыми границами."""
    data = measurement_snapshot("1", "500")
    fact = Fact(id="f1", name="", value="200", unit="метров", raw="200 метров", span=(0, 10), source="text")
    issues = implausible_measurements(data, tuple(data.positions.values()),
                                      [type("C", (), {"ktru_code": "H1"})()], [fact], set())
    assert len(issues) == 1
    assert issues[0].code == "implausible_value" and issues[0].severity == "error"
    # Границы показываются в единице пользователя: 1-500 мм = 0.001-0.5 м.
    assert "от 0.001 до 0.5 м" in issues[0].explanation


def test_plausible_measurement_is_silent():
    """То же значение в допустимом диапазоне — замечаний нет."""
    data = measurement_snapshot("1", "500000")
    fact = Fact(id="f1", name="", value="200", unit="метров", raw="200 метров", span=(0, 10), source="text")
    assert implausible_measurements(data, tuple(data.positions.values()),
                                    [type("C", (), {"ktru_code": "H1"})()], [fact], set()) == []


def test_measurement_check_follows_the_data():
    """Меняем диапазон позиции — меняется решение: предел не зашит в код."""
    fact = Fact(id="f1", name="", value="200", unit="метров", raw="200 метров", span=(0, 10), source="text")
    candidates = [type("C", (), {"ktru_code": "H1"})()]
    strict = measurement_snapshot("1", "500")
    loose = measurement_snapshot("1", "1000000")
    assert implausible_measurements(strict, tuple(strict.positions.values()), candidates, [fact], set())
    assert not implausible_measurements(loose, tuple(loose.positions.values()), candidates, [fact], set())


def test_measurement_of_another_dimension_is_not_judged():
    """Масса не судится по диапазонам длины: сравниваются только совпадающие размерности."""
    data = measurement_snapshot("1", "500")
    fact = Fact(id="f1", name="", value="200", unit="кг", raw="200 кг", span=(0, 6), source="text")
    assert implausible_measurements(data, tuple(data.positions.values()),
                                    [type("C", (), {"ktru_code": "H1"})()], [fact], set()) == []


def test_rejected_fact_is_not_judged():
    """Отклонённый факт не проверяем: пользователь его уже убрал."""
    data = measurement_snapshot("1", "500")
    fact = Fact(id="f1", name="", value="200", unit="метров", raw="200 метров", span=(0, 10), source="text")
    assert implausible_measurements(data, tuple(data.positions.values()),
                                    [type("C", (), {"ktru_code": "H1"})()], [fact], {"f1"}) == []
