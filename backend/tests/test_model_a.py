"""Модель A — нормализатор запроса (`docs/MODEL.md` §4.1): текст → структура.

Проверяются ступени уверенности, порог жёсткости контракта, ограничение по
размерности единицы, словарная сегментация, разбор значения и правила разрешения
`def_id` — то, на что опирается контракт C/D.

Модуль `app/model_a.py` пока не входит в git (см. `docs/ML_STATE.md` §2), поэтому
файл целиком пропускается там, где его нет: тесты не должны ломать сборку на
чистом клоне.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

model_a = pytest.importorskip("app.model_a", reason="модуль модели A не входит в репозиторий")

from app.catalog import AllowedValue, Position, Property  # noqa: E402


def prop(identifier, name, unit=None, aliases=(), value_type="number"):
    allowed = ()
    if unit:
        allowed = (AllowedValue(kind="interval", lower="0", upper="10", unit=unit,
                                raw_unit=unit, source="test"),)
    return Property(id=identifier, name=name, aliases=tuple(aliases),
                    value_type=value_type, allowed=allowed)


def pos(code, *properties, name=None):
    return Position(code=code, name=name or code, source_path="test",
                    properties=tuple(properties))


def test_confidence_tiers_are_ordered_and_partial_is_below_the_hard_threshold():
    """Ступени уверенности упорядочены, и только `partial` ниже порога контракта."""
    tiers = model_a.CONFIDENCE
    assert tiers["exact"] > tiers["lemma"] > tiers["alias_lemma"] > tiers["partial"]
    assert tiers["partial"] < model_a.HARD_FACT_MIN_CONFIDENCE <= tiers["alias_lemma"]
    assert model_a.HARD_FACT_MIN_CONFIDENCE == 0.6


def test_strong_matches_are_hard_partial_match_is_soft():
    """Точное, лемматизированное и по алиасу совпадения жёсткие, частичное — нет."""
    exact = model_a.match_in_position(pos("p", prop("c1", "Мощность")), "Мощность")[0]
    lemma = model_a.match_in_position(pos("p", prop("c1", "Мощность")), "мощности")[0]
    alias = model_a.match_in_position(
        pos("p", prop("c1", "Мощность", aliases=("Мощность двигателя",))),
        "мощности двигателя")[0]
    partial = model_a.match_in_position(
        pos("p", prop("c1", "Мощность двигателя")), "Мощность")[0]

    assert (exact.method, lemma.method, alias.method, partial.method) == \
        ("exact", "lemma", "alias_lemma", "partial")
    assert exact.hard and lemma.hard and alias.hard
    assert not partial.hard


def test_unit_dimension_guard_blocks_a_contradicting_unit():
    """«Масса» не сопоставляется с фактом в миллиметрах, но совпадает в килограммах."""
    mass = pos("p", prop("c1", "Масса", unit="кг"))
    assert model_a.match_in_position(mass, "Масса", "мм") == []
    assert model_a.match_in_position(mass, "Масса", "кг")[0].method == "exact"
    # Неизвестная справочнику единица не доказательство несовместимости.
    assert model_a.match_in_position(mass, "Масса", "фунт")[0].method == "exact"
    # У характеристики без единиц ограничение не применяется.
    plain = pos("p", prop("c1", "Масса"))
    assert model_a.match_in_position(plain, "Масса", "мм")[0].method == "exact"


def test_is_empty_value_drops_parentheses_and_junk():
    assert model_a.is_empty_value("(комплект)")
    assert model_a.is_empty_value("")
    assert model_a.is_empty_value("х")
    assert not model_a.is_empty_value("Полиэстер")
    assert not model_a.is_empty_value("Нет")
    # Известное ограничение: правило «две буквы и меньше» отбрасывает и короткий
    # содержательный ответ. Тест фиксирует текущее поведение, а не желаемое.
    assert model_a.is_empty_value("Да")


def test_split_by_known_names_prefers_the_longest_name():
    known = model_a.known_names_from_variants(["Высота", "Высота упаковки"])
    assert model_a.split_by_known_names("Высота упаковки 200 мм", known) == ("Высота упаковки", "200 мм")
    assert model_a.split_by_known_names("Высота 200 мм", known) == ("Высота", "200 мм")
    # Сравнение по леммам: падежная форма находит имя.
    assert model_a.split_by_known_names("высоты 200 мм", known) == ("Высота", "200 мм")
    assert model_a.split_by_known_names("Неизвестное имя 5", known) is None


def test_parse_fragment_extracts_name_value_and_unit():
    assert model_a.parse_fragment("Вес 2.03000 кг") == \
        model_a.Parsed(name="Вес", value="2.03000", unit="кг", kind="number")
    # Без словаря «Тип застежки Молния» не делится: ключ сам содержит пробелы.
    guess = model_a.parse_fragment("Тип застежки Молния")
    assert guess.name == "" and guess.reason == "no_name"
    known = model_a.known_names_from_variants(["Тип застежки"])
    assert model_a.parse_fragment("Тип застежки Молния", known) == \
        model_a.Parsed(name="Тип застежки", value="Молния", unit=None, kind="text")
    assert model_a.parse_fragment("") == model_a.Parsed("", "", None, "text", "empty")


def test_to_number_accepts_comma_spaces_and_unicode_minus():
    assert model_a.to_number("2,5") == 2.5
    assert model_a.to_number("−3") == -3.0
    assert model_a.to_number("1 000") == 1000.0
    assert model_a.to_number("abc") is None


def test_resolve_gives_def_id_only_when_it_is_the_same_across_the_pool():
    """Коды характеристик переиспользуются между позициями семьи — это не неоднозначность."""
    parsed = model_a.Parsed(name="Высота", value="750", unit="мм", kind="number")
    same = [pos("a", prop("c1", "Высота", unit="мм")), pos("b", prop("c1", "Высота", unit="мм"))]
    resolution = model_a.resolve(parsed, same)
    assert resolution.def_id == "c1" and resolution.method == "exact"
    assert resolution.resolvable_per_position

    different = [pos("a", prop("c1", "Высота", unit="мм")), pos("b", prop("c2", "Высота", unit="мм"))]
    resolution = model_a.resolve(parsed, different)
    assert resolution.def_id == "c1"                      # самый частый код пула
    assert [item["def_id"] for item in resolution.alternatives] == ["c2"]
    assert not resolution.ambiguous_in

    ambiguous = [pos("a", prop("c1", "Высота", unit="мм"), prop("c2", "Высота", unit="мм"))]
    resolution = model_a.resolve(parsed, ambiguous)
    assert resolution.ambiguous_in == ["a"]
    assert not resolution.resolvable_per_position


def test_partial_match_does_not_rewrite_the_name():
    """«Цвет» не должен превращаться в «Цвет мела»: подстановка обнуляла пул."""
    parsed = model_a.Parsed(name="Цвет", value="синий", unit=None, kind="text")
    resolution = model_a.resolve(parsed, [pos("a", prop("c1", "Цвет мела", value_type="text"))])
    assert resolution.method == "partial"
    assert resolution.canonical_name == "Цвет"


def test_facts_from_fragments_returns_contract_facts():
    positions = [pos("a", prop("c1", "Высота", unit="мм"), prop("c2", "Тип застежки", value_type="text"))]
    known = model_a.known_names(positions)
    facts, stats = model_a.facts_from_fragments(
        ["Высота 750 мм", "Тип застежки Молния", "Тип застежки (комплект)", "Просто текст без цифр"],
        positions, known)

    assert stats["parsed"] == 2
    assert stats["dropped_empty_value"] == 1      # имя разобрано, значение — уточнение
    assert stats["unparsed"] == 1                 # ни имени, ни пары
    height, fastener = facts
    assert height.kind == "number" and height.value == 750.0
    assert height.unit.code == "003" and height.unit.text == "мм"
    assert height.def_id == "c1" and height.fact_id == "ste:0"
    assert height.extraction_method == "fragment+exact"
    assert fastener.kind == "text" and fastener.value == "Молния" and fastener.def_id == "c2"


def test_low_confidence_facts_stay_below_the_contract_threshold():
    """Частичное совпадение уходит фактом, но мягким: C по нему не исключает код."""
    positions = [pos("a", prop("c1", "Мощность двигателя", unit="Вт"))]
    facts, _ = model_a.facts_from_fragments(["Мощность 650 Вт"], positions, model_a.known_names(positions))
    assert len(facts) == 1
    assert facts[0].extraction_confidence == model_a.CONFIDENCE["partial"]
    assert facts[0].extraction_confidence < model_a.HARD_FACT_MIN_CONFIDENCE


def test_unit_codes_cover_the_whole_supply_not_only_supported_ones():
    """Карта единиц модели A шире узкого `OKEI_SUPPORTED`: 161 написание, 126 кодов."""
    assert len(model_a.UNIT_CODES) == 161
    assert len(set(model_a.UNIT_CODES.values())) == 126
    assert model_a.UNIT_CODES["мм"] == "003"
    assert model_a.UNIT_CODES["°с"] == "280"
    assert model_a.UNIT_CODES["дб"] == "3135"
    # Два кода, которые в чужой таблице UNIT_HINTS перепутаны: 293 — гигагерц,
    # 298 — мегапаскаль; 245 — киловатт-час, 263 — ампер-час.
    assert model_a.UNIT_CODES["мпа"] == "298"
    assert model_a.UNIT_CODES["а·ч"] == "263"

    supply = Path(__file__).resolve().parent.parent / "algorithms" / "data" / "okei_units.json"
    if supply.exists():
        codes = {unit["code"] for unit in json.loads(supply.read_text(encoding="utf-8"))["units"]}
        assert set(model_a.UNIT_CODES.values()) <= codes
