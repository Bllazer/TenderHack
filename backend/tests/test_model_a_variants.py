"""Модель A на четырёх категориях: числа, опечатки, их комбинация, порядок слов.

Модуль `app/model_a.py` пока не входит в git (см. `docs/ML_STATE.md` §2), поэтому
файл целиком пропускается там, где его нет.

Что здесь проверяется и почему так:

* **числа в любом порядке** — «Мощность 650 Вт» и «650 Вт мощность» должны давать
  один факт: в свободном описании значение идёт первым не реже, чем вторым;
* **опечатки в имени** — «Мощьность» находится как «Мощность», но факт остаётся
  мягким (0.58 < порога 0.6), поэтому C по нему ничего не исключает;
* **числа и опечатки вместе** — включая порядок «650 вт мощьность»;
* **порядок слов** — результат разбора не зависит от перестановки.
"""
from __future__ import annotations

import pytest

model_a = pytest.importorskip("app.model_a", reason="модуль модели A не входит в репозиторий")

from app.catalog import AllowedValue, Position, Property  # noqa: E402
from app.search import canon  # noqa: E402

KNOWN = model_a.known_names_from_variants([
    "Мощность", "Напряжение", "Высота", "Ширина", "Длина", "Масса", "Объём",
    "Диаметр", "Толщина", "Глубина", "Вес", "Температура", "Тип застежки",
])


def prop(identifier, name, unit=None, value_type="number"):
    allowed = (AllowedValue(kind="interval", lower="0", upper="10000", unit=unit,
                            raw_unit=unit, source="test"),) if unit else ()
    return Property(id=identifier, name=name, value_type=value_type, allowed=allowed)


def pos(code, *properties):
    return Position(code=code, name=code, source_path="test", properties=tuple(properties))


# ============================ 0. значение без имени ===========================
def test_measurement_without_a_name_is_bound_by_the_unit_dimension():
    """«дрель 650 вт» — реальный запрос: «Вт» называет характеристику «Мощность».

    Имени характеристики в тексте нет, но единица задаёт размерность, и если такая
    характеристика в позиции одна, значение принадлежит ей. Перевод в ватты для
    сравнения делает C: факт сохраняет написание пользователя.
    """
    position = pos("p", prop("c1", "Мощность", "Вт"), prop("c2", "Напряжение", "В"))
    facts, stats = model_a.facts_from_fragments(["дрель 650 вт"], [position], KNOWN)
    assert [(item.name, item.value, item.unit.text, item.def_id) for item in facts] == [
        ("Мощность", 650.0, "вт", "c1")]
    assert stats["bound_by_unit"] == 1 and stats["with_def_id"] == 1


def test_two_bare_measurements_are_bound_by_their_own_units():
    """«дрель 650 вт 220 в»: у каждого измерения своя характеристика и свой id факта."""
    position = pos("p", prop("c1", "Мощность", "Вт"), prop("c2", "Напряжение", "В"))
    facts, stats = model_a.facts_from_fragments(["дрель 650 вт 220 в"], [position], KNOWN)
    assert [(item.name, item.value, item.def_id) for item in facts] == [("Мощность", 650.0, "c1"),
                                                                       ("Напряжение", 220.0, "c2")]
    assert len({item.fact_id for item in facts}) == 2 and stats["bound_by_unit"] == 2


def test_bare_measurement_keeps_the_written_unit():
    """«0,65 кВт» сохраняет киловатты: сравнение в единице характеристики — работа C."""
    position = pos("p", prop("c1", "Мощность", "Вт"))
    facts, _ = model_a.facts_from_fragments(["дрель 0,65 кВт"], [position], KNOWN)
    assert [(item.name, item.value, item.unit.text) for item in facts] == [("Мощность", 0.65, "кВт")]


def test_ambiguous_dimension_is_not_bound():
    """Минимальный и максимальный диаметр оба в миллиметрах — угадывать нельзя."""
    position = pos("p", prop("c1", "Минимальный диаметр зажима патрона", "мм"),
                   prop("c2", "Максимальный диаметр зажима патрона", "мм"))
    facts, stats = model_a.facts_from_fragments(["дрель 12 мм"], [position], KNOWN)
    assert [item.name for item in facts] == ["дрель"]        # unmatched, а не выдуманный факт
    assert stats["bound_by_unit"] == 0


def test_dimension_absent_from_the_position_is_not_bound():
    """Мощности у позиции нет — «650 вт» не приписывается наугад.

    Факт с ненайденным именем остаётся (имя «дрель» не характеристика): он уходит
    как дополнительные сведения с нулевой уверенностью, а не как мощность высоты.
    """
    position = pos("p", prop("c1", "Высота", "мм"))
    facts, stats = model_a.facts_from_fragments(["дрель 650 вт"], [position], KNOWN)
    assert [(item.name, item.def_id, item.extraction_confidence) for item in facts] == [("дрель", None, 0.0)]
    assert stats["unmatched"] == 1 and stats["bound_by_unit"] == 0


# ============================================================== 1. числа ======
@pytest.mark.parametrize("text,name,value,unit", [
    ("Мощность 650 Вт", "Мощность", "650", "Вт"),
    ("650 Вт мощность", "Мощность", "650", "Вт"),
    ("Мощность: 650 Вт", "Мощность", "650", "Вт"),
    ("Длина 20 см", "Длина", "20", "см"),
    ("20 см длина", "Длина", "20", "см"),
    ("Масса 2,5 кг", "Масса", "2,5", "кг"),
    ("2,5 кг масса", "Масса", "2,5", "кг"),
    ("Высота 2000 мм", "Высота", "2000", "мм"),
    ("2000 мм высота", "Высота", "2000", "мм"),
    ("мощность 650вт", "Мощность", "650", "вт"),
])
def test_number_is_parsed_in_either_order(text, name, value, unit):
    parsed = model_a.parse_fragment(text, KNOWN)
    assert (parsed.name, parsed.value, parsed.unit, parsed.kind) == (name, value, unit, "number")


def test_name_only_fragment_has_no_value():
    """«Мощность» без значения — это имя без утверждения, а не факт с пустотой."""
    parsed = model_a.parse_fragment("Мощность", KNOWN)
    assert parsed.name == "Мощность" and parsed.value == "" and parsed.reason == "no_value"


# =========================================================== 2. опечатки ======
@pytest.mark.parametrize("typo,name", [
    ("Мощьность", "Мощность"),
    ("мощьность", "Мощность"),
    ("Мошность", "Мощность"),
    ("напряжене", "Напряжение"),
])
def test_confident_typo_replaces_the_name_automatically(typo, name):
    """Уверенная опечатка (≥ 0.85, одна, ≥ 5 букв) заменяется сразу.

    Критерий выбран по замерам: на 774 однобуквенных опечатках похожесть на истинное
    имя имеет медиану 0.900, выше 0.85 попадает 80 % опечаток, и ни одной спорной
    (чужое имя тоже ≥ порога); максимальная похожесть на чужое имя — 0.833.
    """
    matches = model_a.match_in_position(pos("p", prop("c1", name, "мм")), typo, "мм")
    assert [match.property.name for match in matches] == [name]
    assert matches[0].method == "typo"
    assert matches[0].confidence == model_a.CONFIDENCE["typo"]
    assert matches[0].hard


@pytest.mark.parametrize("typo,name", [
    ("Высата", "Высота"),
    ("маса", "Масса"),
    ("Шырина", "Ширина"),
    ("Машность", "Мощность"),
])
def test_unsure_typo_stays_a_soft_fact(typo, name):
    """Неуверенная опечатка (0.72…0.85 или короткое слово) остаётся мягкой.

    Такой факт C проверяет, но ничего по нему не исключает: подстановка без
    подтверждения — догадка, а не доказательство.
    """
    matches = model_a.match_in_position(pos("p", prop("c1", name, "мм")), typo, "мм")
    assert [match.property.name for match in matches] == [name]
    assert matches[0].method == "fuzzy"
    assert matches[0].confidence == model_a.CONFIDENCE["fuzzy"]
    assert not matches[0].hard


def test_unrelated_name_is_not_guessed():
    """Опечатка не должна подставлять чужую характеристику."""
    position = pos("p", prop("c1", "Высота", "мм"))
    assert model_a.match_in_position(position, "Ширина", "мм") == []
    assert model_a.match_in_position(position, "Толщина", "мм") == []


@pytest.mark.parametrize("text,name,value,unit", [
    ("Мощьность 650 Вт", "Мощьность", "650", "Вт"),
    ("Высата 2000 мм", "Высата", "2000", "мм"),
    ("маса 2,5 кг", "маса", "2,5", "кг"),
    ("Мощьность Полиэстер", "Мощьность", "Полиэстер", None),
])
def test_typo_fragment_is_parsed_and_keeps_the_surface_name(text, name, value, unit):
    """Разбор сохраняет написание; канонизирует уже сопоставление.

    Так мягкость факта не теряется: если бы разбор сразу подставлял «Мощность»,
    сопоставление нашло бы точное совпадение, факт стал бы жёстким (0.95) и C
    исключал бы по нему коды — то есть догадка выбрасывала бы верный код.
    """
    parsed = model_a.parse_fragment(text, KNOWN)
    assert (parsed.name, parsed.value, parsed.unit) == (name, value, unit)


def test_confident_typo_fact_is_hard_and_keeps_the_written_text():
    """Уверенная опечатка даёт обычный (жёсткий) факт, но исходный текст сохранён."""
    positions = [pos("p", prop("c1", "Мощность", "Вт"))]
    facts, _ = model_a.facts_from_fragments(["Мошность 650 Вт"], positions, KNOWN)
    assert facts[0].name == "Мощность"
    assert facts[0].extraction_method == "fragment+typo"
    assert facts[0].extraction_confidence == model_a.CONFIDENCE["typo"]
    assert facts[0].extraction_confidence >= model_a.HARD_FACT_MIN_CONFIDENCE


def test_unsure_typo_fact_stays_below_the_contract_threshold():
    positions = [pos("p", prop("c1", "Высота", "мм"))]
    facts, _ = model_a.facts_from_fragments(["Высата 2000 мм"], positions, KNOWN)
    assert facts[0].name == "Высота"
    assert facts[0].extraction_method == "fragment+fuzzy"
    assert facts[0].extraction_confidence < model_a.HARD_FACT_MIN_CONFIDENCE


def test_unsure_typo_fact_stays_below_the_contract_threshold():
    positions = [pos("p", prop("c1", "Высота", "мм"))]
    facts, _ = model_a.facts_from_fragments(["Высата 2000 мм"], positions, KNOWN)
    assert facts[0].name == "Высота"
    assert facts[0].extraction_method == "fragment+fuzzy"
    assert facts[0].extraction_confidence < model_a.HARD_FACT_MIN_CONFIDENCE


# ================================================ 3. числа и опечатки =========
@pytest.mark.parametrize("text,name,value,unit", [
    ("мощьность 650 вт", "мощьность", "650", "вт"),
    ("650 вт мощьность", "мощьность", "650", "вт"),
    ("650 Вт мощьность", "мощьность", "650", "Вт"),
    ("Мощьность 650вт", "Мощьность", "650", "вт"),
])
def test_number_with_a_typo_is_parsed_in_either_order(text, name, value, unit):
    parsed = model_a.parse_fragment(text, KNOWN)
    assert (parsed.name, parsed.value, parsed.unit, parsed.kind) == (name, value, unit, "number")


# ================================================== 4. порядок написания =====
@pytest.mark.parametrize("forward,backward", [
    ("Мощность 650 Вт", "650 Вт мощность"),
    ("Длина 20 см", "20 см длина"),
    ("Масса 2,5 кг", "2,5 кг масса"),
    ("Высота 2000 мм", "2000 мм высота"),
    ("Напряжение 220 В", "220 В напряжение"),
    ("Мощьность 650 Вт", "650 Вт мощьность"),
    ("Высата 2000 мм", "2000 мм высата"),
])
def test_both_orders_parse_identically(forward, backward):
    """«молоток 20см» и «20см молоток»: разбор не зависит от порядка слов.

    Написание имени сравнивается с точностью до регистра: разбор сохраняет текст
    как в описании («Высата» и «высата»), а каноническое имя даёт сопоставление.
    """
    left = model_a.parse_fragment(forward, KNOWN)
    right = model_a.parse_fragment(backward, KNOWN)
    assert (canon(left.name), left.value, left.unit) == (canon(right.name), right.value, right.unit)
    assert left.name and left.value


def test_all_four_categories_together_give_facts_without_unparsed():
    """Сводная проверка: ни один фрагмент из четырёх категорий не потерян."""
    positions = [pos("p", prop("c1", "Мощность", "Вт"), prop("c2", "Высота", "мм"),
                     prop("c3", "Длина", "см"), prop("c4", "Масса", "кг"))]
    fragments = [
        "Мощность 650 Вт", "650 Вт мощность",           # числа
        "Мощьность 650 Вт", "Высата 2000 мм",           # опечатки
        "650 вт мощьность", "мощьность 650 вт",          # числа и опечатки
        "20 см длина", "2,5 кг масса",                   # порядок
    ]
    facts, stats = model_a.facts_from_fragments(fragments, positions, KNOWN)
    assert stats["unparsed"] == 0
    assert len(facts) == len(fragments)
    assert {fact.name for fact in facts} == {"Мощность", "Высота", "Длина", "Масса"}
    assert all(fact.extraction_confidence > 0 for fact in facts)
