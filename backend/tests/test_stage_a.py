"""Тесты стадии A (текст → структура) по требованиям независимого ревью A1–A12.

Все данные синтетические: полный каталог, E5 и сеть не нужны. Факты C проверяются
настоящим контрактом (`algorithms/contract.py`): `is_hard` — это ровно тот признак,
по которому C исключает код, поэтому «факт не должен попасть в жёсткие» проверяется
буквально, а не по косвенным признакам.
"""
from __future__ import annotations

import pytest

from algorithms.contract import Fact as ContractFact
from app.catalog import AllowedValue, Position, Property
from app.extraction import analyzer, extract, lemmas
from app.schemas import Fact
from app.search import canon
from app.structured import structure


# --------------------------------------------------------------------- сборка
def allowed(value: str) -> AllowedValue:
    return AllowedValue(kind="enum", value=value, source="test")


def prop(identifier: str, name: str, value_type: str = "text", kind: str = "1",
         allowed_values: tuple[AllowedValue, ...] = (), aliases: tuple[str, ...] = ()) -> Property:
    return Property(id=identifier, name=name, aliases=aliases, value_type=value_type, kind=kind,
                    required=True, verified=True, evidence="test", allowed=allowed_values)


def position(*properties: Property) -> Position:
    return Position(code="TEST-POSITION", name="Тестовая позиция", source_path="test", properties=properties)


def fact(value: str, name: str = "Высота", unit: str | None = None, source: str = "text", unknown: bool = False,
         raw: str | None = None, span: tuple[int, int] | None = None, negated: bool = False,
         identifier: str = "fact:1") -> Fact:
    return Fact(id=identifier, name=name, value=value, unit=unit, raw=raw if raw is not None else f"{name}: {value}",
                span=span, source=source, negated=negated, unknown=unknown)


def hard(item) -> bool:
    """Жёсткий ли факт для C: контракт должен быть валиден, а `is_hard` — решать."""
    parsed = ContractFact.from_dict(item.model_dump())
    assert parsed.problems() == [], parsed.problems()
    return parsed.is_hard


POWER = prop("power", "Мощность", value_type="number", aliases=("мощностью",))


# ------------------------------------------------------------------ A1: границы
def test_closed_bracket_interval_stays_hard():
    """[1;2] — обе границы замкнуты, это выразимо в контракте: факт остаётся жёстким."""
    item = structure(fact("[1;2]"))
    assert item.kind == "range"
    assert item.value == {"min": 1, "max": 2, "min_inclusive": True, "max_inclusive": True}
    assert item.polarity == "affirmed"
    assert item.extraction_confidence >= 0.6
    assert hard(item)


def test_closed_point_interval_stays_hard():
    assert hard(structure(fact("[750;750]")))
    assert structure(fact("[750;750]")).value == {"min": 750, "max": 750,
                                                  "min_inclusive": True, "max_inclusive": True}


@pytest.mark.parametrize("value,bounds", [
    ("(1;2)", {"min": 1, "max": 2, "min_inclusive": False, "max_inclusive": False}),
    ("[1;2)", {"min": 1, "max": 2, "min_inclusive": True, "max_inclusive": False}),
    ("(1;2]", {"min": 1, "max": 2, "min_inclusive": False, "max_inclusive": True}),
    ("(1,5;2,5)", {"min": 1.5, "max": 2.5, "min_inclusive": False, "max_inclusive": False}),
])
def test_strict_boundaries_are_kept_and_stay_hard(value, bounds):
    """Открытые границы сохраняются, и факт остаётся жёстким.

    `min_inclusive`/`max_inclusive` — согласованная с C нотация:
    `AlgorithmC.check_fact` читает эти ключи, поэтому строгий интервал выразим
    точно и не должен ослабляться до `uncertain`.
    """
    item = structure(fact(value))
    assert item.kind == "range"
    assert item.value == bounds
    assert item.polarity == "affirmed"
    assert item.extraction_confidence >= 0.6
    assert hard(item)


def test_strict_interval_from_extraction_keeps_text_and_span():
    facts, _ = extract("Высота: (1;2) мм", ("Высота",))
    assert [item.value for item in facts] == ["(1;2)"]
    assert facts[0].unit == "мм"
    item = structure(facts[0])
    assert item.value == {"min": 1, "max": 2, "min_inclusive": False, "max_inclusive": False}
    assert item.raw == "Высота: (1;2) мм"
    assert hard(item)


# -------------------------------------------------- A2: перевёрнутый интервал
def test_reversed_interval_is_not_swapped_and_is_not_hard():
    """[8;2] — ошибка ввода, а не [2;8]: границы не переставляются.

    Такой `range` контракт отвергает (`problems()`), поэтому факт уходит текстом с
    исходным написанием: он не исключает код и не подменяет введённое значение.
    """
    item = structure(fact("[8;2]"))
    assert item.kind == "text"
    assert item.value == "[8;2]"
    assert "8" in item.value and item.value.index("8") < item.value.index("2")
    assert item.polarity == "uncertain"
    assert item.extraction_confidence < 0.6
    assert item.extraction_method == "reversed_interval"
    assert not hard(item)


def test_reversed_interval_survives_extraction_unchanged():
    facts, _ = extract("Высота: [8;2] мм", ("Высота",))
    assert facts[0].value == "[8;2]"
    assert facts[0].raw == "Высота: [8;2] мм"
    assert not hard(structure(facts[0]))


# -------------------------------------------------------- A3: пустой интервал
@pytest.mark.parametrize("value", ["(750;750)", "(750;750]", "[750;750)"])
def test_empty_interval_is_not_a_point(value):
    """low == high и хотя бы одна граница строгая — интервал пуст, а не точка 750."""
    item = structure(fact(value))
    assert item.kind != "number"
    assert item.value != 750
    assert item.polarity == "uncertain"
    assert item.extraction_confidence < 0.6
    assert item.extraction_method == "empty_interval"
    assert not hard(item)


def test_empty_interval_from_extraction_keeps_text():
    facts, _ = extract("Высота: (750;750) мм", ("Высота",))
    item = structure(facts[0])
    assert item.kind == "text" and item.value == "(750;750)"
    assert item.raw == "Высота: (750;750) мм"
    assert item.extraction_method == "empty_interval"
    assert not hard(item)


# ------------------------------------------------------- A4/A5: неизвестное
def test_unknown_text_value_is_uncertain():
    facts, _ = extract("Мощность: не знаю", ("Мощность",))
    assert facts[0].unknown
    item = structure(facts[0])
    assert item.polarity == "uncertain"
    assert item.extraction_confidence < 0.6
    assert item.extraction_method == "unknown_value"
    assert not hard(item)


@pytest.mark.parametrize("value", ["", "не знаю", "не задано", "(не задано)"])
def test_unknown_placeholder_is_uncertain(value):
    item = structure(fact(value, unknown=True))
    assert item.polarity == "uncertain"
    assert item.extraction_confidence < 0.6
    assert not hard(item)


@pytest.mark.parametrize("value", ["", "не знаю", "не задано", "неизвестно"])
@pytest.mark.parametrize("source", ["text", "answer", "input"])
def test_unknown_placeholder_is_uncertain_without_the_flag(value, source):
    """Значение-заглушка не подтверждается и БЕЗ флага `unknown`.

    `app/analysis.py` выставляет `unknown` только при пустом значении, поэтому
    ответ пользователя «не знаю», набранный текстом, приходит с `unknown=False`.
    Если это не распознать, ответ становится жёстким фактом и исключает коды.
    """
    item = structure(fact(value, unit="мм", source=source))
    assert item.polarity == "uncertain"
    assert item.extraction_confidence < 0.6
    assert item.extraction_method == "unknown_value"
    assert not hard(item)


def test_source_input_or_answer_never_confirms_unknown():
    """Источник «input»/«answer» не делает неизвестный факт уверенным."""
    for source in ("input", "answer"):
        item = structure(fact("", unit="Вт", source=source, unknown=True))
        assert item.polarity == "uncertain"
        assert item.extraction_confidence < 0.6
        assert not hard(item)


def test_source_input_with_value_stays_confident():
    item = structure(fact("650", unit="Вт", source="input"))
    assert item.polarity == "affirmed"
    assert item.extraction_confidence == 1.0
    assert item.extraction_method == "user_input"
    assert hard(item)


def test_lemma_form_gets_the_characteristic_code():
    """«мощностью 650 Вт» — проверенная словарём форма «Мощность»: код должен привязаться."""
    properties = position(prop("power", "Мощность", value_type="number"))
    item = structure(fact("650", name="мощностью", unit="Вт"), properties)
    assert item.def_id == "power"
    # Написание из текста сохраняется в raw; подмены по похожести нет.
    assert item.raw == "мощностью: 650"
    # Похожее, но другое написание код не получает — автопочинки нет.
    assert structure(fact("650", name="Мошность", unit="Вт"), properties).def_id is None


def test_ambiguous_lemma_form_binds_nothing():
    """Если под леммы подходит несколько характеристик, код не выбирается."""
    properties = position(prop("a", "Мощность", value_type="number"),
                          prop("b", "Мощность", value_type="number"))
    assert structure(fact("650", name="мощностью", unit="Вт"), properties).def_id is None


# ------------------------------------------- A6: «да/нет» по характеристике
def test_boolean_kind_comes_from_characteristic():
    boolean = prop("presence", "Наличие", value_type="boolean",
                   allowed_values=(allowed("да"), allowed("нет")))
    target = position(boolean)
    assert structure(fact("да", name="Наличие"), target).kind == "boolean"
    assert structure(fact("да", name="Наличие"), target).value is True
    assert structure(fact("нет", name="Наличие"), target).value is False
    assert structure(fact("НЕТ", name="Наличие"), target).value is False


def test_text_property_with_da_net_pair_is_boolean_too():
    pair = prop("pair", "Наличие", value_type="text", allowed_values=(allowed("да"), allowed("нет")))
    item = structure(fact("да", name="Наличие"), position(pair))
    assert item.kind == "boolean" and item.value is True


def test_boolean_is_not_invented_without_the_pair():
    """Допустимое «Да» как обычный текст без пары — не булево."""
    single = prop("mark", "Маркировка", value_type="text", allowed_values=(allowed("Да"),))
    item = structure(fact("Да", name="Маркировка"), position(single))
    assert item.kind == "text" and item.value == "Да"


def test_boolean_is_not_invented_without_position():
    assert structure(fact("да", name="Наличие")).kind == "text"


def test_text_value_is_not_turned_into_boolean_by_other_property():
    number = prop("power", "Мощность", value_type="number")
    assert structure(fact("да", name="Мощность"), position(number)).kind == "text"


# ------------------------------------------------ A7: raw, span, id, единица
def test_raw_span_fact_id_and_unit_are_preserved():
    original = fact("650", name="Мощность", unit="Вт", raw="Мощность: 650 Вт", span=(0, 16),
                    identifier="text:0:16:0")
    item = structure(original)
    assert item.raw == "Мощность: 650 Вт"
    assert item.span == [0, 16]
    assert item.fact_id == "text:0:16:0"
    assert item.unit is not None and item.unit.text == "Вт" and item.unit.code == "212"


def test_unrecognized_unit_keeps_spelling_without_okei_code():
    item = structure(fact("5", name="Длина", unit="футов"))
    assert item.unit is not None
    assert item.unit.text == "футов"
    assert item.unit.code is None


# ----------------------------------------------------- A8: леммы и «мощностью»
def test_property_name_is_found_by_lemma_not_substring():
    """Форма слова найдена по леммам, имя приведено к справочнику, текст сохранён."""
    facts, _ = extract("Мощностью 650 Вт", ("Мощность",))
    assert len(facts) == 1
    assert facts[0].name == "Мощность"
    assert facts[0].value == "650"
    assert facts[0].unit == "Вт"
    assert facts[0].raw == "Мощностью 650 Вт"
    assert facts[0].span == (0, 16)


@pytest.mark.parametrize("text", ["мощности 650 Вт", "Мощность 650 Вт"])
def test_other_lemma_forms_are_found_too(text):
    facts, _ = extract(text, ("Мощность",))
    assert [item.value for item in facts] == ["650"]
    assert facts[0].unit == "Вт"


def test_multiword_property_name_is_found_by_lemmas():
    facts, _ = extract("Тип патрона быстросъёмный", ("Тип патрона",))
    assert len(facts) == 1
    assert facts[0].value == "быстросъёмный"


def test_longest_name_wins_and_overlaps_do_not_duplicate():
    text = "Высота упаковки 750 мм"
    facts, _ = extract(text, ("Высота", "Высота упаковки"))
    assert len(facts) == 1
    assert canon(facts[0].name) == "высота упаковки"
    assert facts[0].value == "750" and facts[0].unit == "мм"
    assert text[slice(*facts[0].span)] == facts[0].raw


def test_negation_still_works_with_other_lemma_forms():
    facts, _ = extract("без наличия режима реверса", ("Наличие режима реверса",))
    assert len(facts) == 1
    assert facts[0].negated and facts[0].value == "нет"
    assert facts[0].raw == "без наличия режима реверса"
    assert structure(facts[0]).polarity == "negated"


def test_lemma_matching_binds_def_id_through_alias():
    facts, _ = extract("Мощностью 650 Вт", ("Мощность",))
    item = structure(facts[0], position(POWER))
    assert item.def_id == "power"
    assert item.kind == "number" and item.value == 650
    assert hard(item)


def test_analyzer_is_a_lazy_module_singleton():
    assert analyzer() is analyzer()
    assert lemmas("Мощностью 650 Вт") == analyzer().tokens("Мощностью 650 Вт")


# ------------------------------------------- A9: «0, 75» не подтверждённый ноль
def test_ambiguous_decimal_separator_never_confirms_zero():
    facts, _ = extract("Высота: 0, 75 м", ("Высота",))
    assert len(facts) == 1
    assert facts[0].unknown
    assert facts[0].value == "0, 75"
    assert facts[0].unit == "м"
    assert facts[0].raw == "Высота: 0, 75 м"
    assert not any(item.value == "0" for item in facts)
    assert not any(item.value == "75" for item in facts)


def test_ambiguous_decimal_separator_is_uncertain_and_keeps_text():
    facts, _ = extract("Высота: 0, 75 м", ("Высота",))
    item = structure(facts[0])
    assert item.kind == "text"
    assert item.value == "0, 75"
    assert item.value != 0.75
    assert item.polarity == "uncertain"
    assert item.extraction_confidence < 0.6
    assert item.extraction_method == "ambiguous_separator"
    assert not hard(item)


def test_plain_decimal_comma_still_confirms():
    facts, _ = extract("Высота: 0,75 м", ("Высота",))
    assert len(facts) == 1 and not facts[0].unknown
    assert facts[0].value == "0,75" and facts[0].unit == "м"
    item = structure(facts[0])
    assert item.kind == "number" and item.value == 0.75
    assert hard(item)


# --------------------------------------------- A10: список значений целиком
def test_comma_separated_text_values_are_kept():
    facts, _ = extract("Цвет: красный, белый", ("Цвет",))
    assert len(facts) == 1
    assert facts[0].value == "красный, белый"
    assert "красный" in facts[0].value and "белый" in facts[0].value
    assert facts[0].raw == "Цвет: красный, белый"


def test_comma_before_new_fact_still_splits():
    facts, _ = extract("Цвет: красный, Оттенок: тёмный", ("Цвет", "Оттенок"))
    assert [item.name for item in facts] == ["Цвет", "Оттенок"]
    assert [item.value for item in facts] == ["красный", "тёмный"]


def test_comma_before_known_property_name_without_colon_still_splits():
    facts, _ = extract("Мощность 650 Вт, Напряжение 220 В", ("Мощность", "Напряжение"))
    assert [item.name for item in facts] == ["Мощность", "Напряжение"]
    assert [item.value for item in facts] == ["650", "220"]


def test_multiple_values_become_text_set_only_for_multiple_choice():
    facts, _ = extract("Цвет: красный, белый", ("Цвет",))
    multiple = prop("color", "Цвет", value_type="text", kind="3",
                    allowed_values=(allowed("красный"), allowed("белый")))
    item = structure(facts[0], position(multiple))
    assert item.kind == "text_set"
    assert item.value == ["красный", "белый"]
    single = prop("color-one", "Цвет", value_type="text", kind="1",
                  allowed_values=(allowed("красный, белый"),))
    kept = structure(facts[0], position(single))
    assert kept.kind == "text"
    assert kept.value == "красный, белый"


# ------------------------------------ A11: неподдержанная единица и фрагменты
def test_unsupported_unit_is_kept_and_never_confirmed():
    facts, _ = extract("Длина: 5 футов", ("Длина",))
    assert len(facts) == 1
    assert facts[0].value == "5"
    assert facts[0].unit == "футов"
    assert facts[0].raw == "Длина: 5 футов"
    item = structure(facts[0], position(prop("length", "Длина", value_type="number")))
    assert item.unit is not None and item.unit.text == "футов" and item.unit.code is None
    assert item.polarity == "uncertain"
    assert item.extraction_confidence < 0.6
    assert item.extraction_method == "unsupported_unit"
    assert not hard(item)


def test_supported_unit_is_not_treated_as_unsupported():
    facts, _ = extract("Длина: 5 м", ("Длина",))
    assert facts[0].unit == "м"
    assert hard(structure(facts[0]))


def test_orphan_measurement_survives_as_unassigned_fact():
    facts, fragments = extract("Длина: 5 футов; 650 Вт", ("Длина",))
    orphan = next(f for f in facts if f.raw == '650 Вт')
    assert orphan.name == '' and orphan.value == '650' and orphan.unit == 'Вт'
    assert orphan.raw == 'Длина: 5 футов; 650 Вт'[slice(*orphan.span)]
    assert all(fragment.raw == "Длина: 5 футов; 650 Вт"[slice(*fragment.span)] for fragment in fragments)


# ------------------- A12: «починка» похожего — два уровня (см. app/extraction)
def test_confident_misspelling_is_repaired_and_reported():
    """«Мошность» (0.875) — уверенная опечатка: имя исправляется автоматически.

    Политика изменена намеренно владельцем: при уверенности
    (`app/extraction.TYPO_CONFIDENT`, 0.85) замена делается сразу, а не вопросом.
    Критерий проверен на 774 однобуквенных опечатках: выше 0.85 попадает 80 % из
    них, и ни одной спорной. Исходное написание не теряется — оно остаётся в `raw`
    факта и в провенансе (`confirmation_required=False`).
    """
    corrections: list = []
    facts, _ = extract("Мошность: 650 Вт", ("Мощность",), corrections=corrections)
    assert len(facts) == 1
    assert facts[0].name == "Мощность"
    assert facts[0].raw == "Мошность: 650 Вт"
    assert [(item.original, item.normalized, item.confirmation_required)
            for item in corrections] == [("Мошность", "Мощность", False)]
    item = structure(facts[0], position(POWER))
    assert item.name == "Мощность"
    assert item.def_id is not None
    assert item.value == 650
    assert item.unit is not None and item.unit.text == "Вт"


def test_unsure_misspelling_is_not_repaired():
    """«Машность» (0.750) — только подсказка: имя остаётся как написано.

    Между `TYPO_SUGGEST` (0.72) и `TYPO_CONFIDENT` (0.85) извлечение не подменяет
    ничего: факт уходит с написанием пользователя, а исправление предлагает поток
    коррекций (`tests/test_correction_http.py`).
    """
    facts, _ = extract("Машность: 650 Вт", ("Мощность",))
    assert len(facts) == 1
    assert facts[0].name == "Машность"
    assert facts[0].raw == "Машность: 650 Вт"
    item = structure(facts[0], position(POWER))
    assert item.name == "Машность"


def test_value_is_not_normalized_to_dictionary():
    color = prop("color", "Цвет", value_type="text", allowed_values=(allowed("красный"), allowed("белый")))
    item = structure(fact("красный", name="Цвет"), position(color))
    assert item.kind == "text" and item.value == "красный"
    assert item.def_id == "color"


def test_facts_survive_round_trip_to_contract():
    """Полезная нагрузка A остаётся валидной для C, включая аддитивное поле raw."""
    original = fact("650", name="Мощность", unit="Вт", raw="Мощность: 650 Вт", span=(0, 16))
    payload = structure(original, position(POWER)).model_dump()
    assert payload["raw"] == "Мощность: 650 Вт"
    parsed = ContractFact.from_dict(payload)
    assert parsed.problems() == []
    assert parsed.unit.text == "Вт" and parsed.unit.code == "212"
