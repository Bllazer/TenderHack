"""Тесты правок по замечаниям ручной проверки владельца (модель B и A).

Быстрые проверки не требуют индекса; проверки на реальном каталоге включаются
переменной `RUN_E5_TESTS=1`, как и остальные измерения поиска.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.catalog import AllowedValue, Property, load_snapshot
from app.extraction import extract
from app.search import (Analyzer, E5Encoder, Search, SIGNIFICANCE, closest_vocabulary_token,
                        edit_distance, letter_overlap, looks_like_typo, transposition_match)

INDEX_DIR = Path(os.environ.get("TEST_INDEX_DIR", "resources/catalog-build"))
MODEL_DIR = Path(os.environ.get("TEST_MODEL_DIR", "resources/model"))


# ---------------------------------------------------------------- быстрые проверки
@pytest.mark.parametrize("left,right,distance", [
    ("дерл", "дрель", 2),          # перестановка + пропущенная буква
    ("дрелль", "дрель", 1),        # лишняя буква
    ("малоток", "молоток", 1),     # замена
    ("шуруповирт", "шуруповерт", 1),
    ("дрель", "дрель", 0),
])
def test_edit_distance_counts_transposition_as_one_edit(left, right, distance):
    """Перестановка соседних букв — одна правка, а не две."""
    assert edit_distance(left, right, 3) == distance


@pytest.mark.parametrize("token,word", [
    ("дерл", "дрель"),        # короче на букву и с перестановкой — так и набирают
    ("малоток", "молоток"),
    ("шуруповирт", "шуруповерт"),
])
def test_typo_word_is_recognised(token, word):
    """Почти написанное слово первого места названия считается опечаткой."""
    assert looks_like_typo(token, word)


@pytest.mark.parametrize("token,word", [
    ("дел", "дрель"),         # короче запроса: пользователь не выбрасывает корень
    ("вкуд", "вод"),          # мало общих букв (латинская раскладка)
    ("шашки", "шпажки"),      # разные слова, совпало только начало
    ("жен", "женское"),       # сокращения сюда не относятся: это ступень перечислений
])
def test_unrelated_word_is_not_a_typo(token, word):
    assert not looks_like_typo(token, word)


def test_letter_overlap_and_transposition_helpers_agree_with_examples():
    assert letter_overlap("дерл", "дрель") == pytest.approx(0.8)
    assert letter_overlap("вкуд", "вод") == pytest.approx(0.5)
    assert transposition_match("дрелль", ["дрель", "дрели"]) is None   # две замены длины 6 → не перестановка
    assert transposition_match("дреьл", ["дрель"]) == "дрель"


def test_closest_vocabulary_token_prefers_the_not_shorter_word():
    """«дерл» → «дрель», а не «дел»: сокращение ищется в сторону полного слова."""
    vocabulary = ("дрель", "дел", "двери")
    assert closest_vocabulary_token("дерл", vocabulary) == "дрель"


def test_significance_threshold_is_calibrated_between_known_bands():
    """Порог значимости лежит в разрыве: нужные семейства ≥0.049, мусор ≤0.033."""
    assert 0.033 <= SIGNIFICANCE <= 0.049


# ------------------------------------------- сокращения значений перечислений (A)
def enum_property(identifier: str, name: str, values: tuple[str, ...]) -> Property:
    return Property(id=identifier, name=name, value_type="text", required=True, verified=True,
                    evidence="test", allowed=tuple(AllowedValue(kind="enum", value=value, source="test")
                                                  for value in values))


def coat_scope() -> tuple[Property, ...]:
    return (enum_property("sex", "Половой признак", ("Мужское", "Женское")),
            enum_property("src", "Тип питания", ("От сети", "От аккумулятора")),
            enum_property("ins", "Тип утеплителя", ("Искусственный утеплитель", "Натуральный утеплитель")))


def coat_names() -> tuple[str, ...]:
    return ("Половой признак", "Тип питания", "Тип утеплителя")


@pytest.mark.parametrize("text,name,value", [
    ("пальто муж 48", "Половой признак", "Мужское"),
    ("пальто жен 48", "Половой признак", "Женское"),
    ("дрель аккум 18 в", "Тип питания", "От аккумулятора"),
    ("дрель от сет 650 вт", "Тип питания", "От сети"),
])
def test_abbreviated_enum_value_is_recognised(text, name, value):
    """Сокращение значения: «муж» → «Мужское», «аккум» → «От аккумулятора».

    Реальный ввод «пальто муж 48»: пол написан сокращённо, и без этой ступени система
    переспрашивала его, хотя ответ в тексте есть.
    """
    facts, _ = extract(text, coat_names(), scoped_properties=coat_scope())
    assert any(fact.name == name and fact.value == value for fact in facts)


@pytest.mark.parametrize("text", [
    "пальто искусственный 48",   # только первое слово оборота: значения ещё нет
    "пальто утеплитель",         # только второе слово, и оно целое, а не сокращение
    "пальто 48",
])
def test_abbreviation_does_not_invent_multiword_values(text):
    """Одно слово многословного значения не становится значением: нужен весь оборот."""
    facts, _ = extract(text, coat_names(), scoped_properties=coat_scope())
    assert not any(fact.name == "Тип утеплителя" for fact in facts)


def test_full_multiword_value_still_works():
    """Целый оборот «искусственный утеплитель» разбирается обычным окном перечислений."""
    facts, _ = extract("пальто искусственный утеплитель", coat_names(), scoped_properties=coat_scope())
    assert any(fact.name == "Тип утеплителя" and "искусственный" in fact.value for fact in facts)


# ------------------------------------------------- проверки на реальном каталоге
@pytest.fixture(scope="module")
def real_search() -> Search:
    if os.environ.get("RUN_E5_TESTS") != "1":
        pytest.skip("нужны реальный индекс и веса e5 (RUN_E5_TESTS=1)")
    if not (INDEX_DIR / "manifest.json").exists() or not (MODEL_DIR / "model.meta.json").exists():
        pytest.skip(f"нет артефактов: {INDEX_DIR} и/или {MODEL_DIR}")
    metadata = json.loads((MODEL_DIR / "model.meta.json").read_text(encoding="utf-8"))
    data = load_snapshot(INDEX_DIR)
    return Search(data, E5Encoder(MODEL_DIR, metadata["revision"]), Analyzer())


@pytest.mark.parametrize("text", ["панамка", "drel", "кофеварка", "фывапр", "абракадабра"])
def test_missing_product_returns_nothing(real_search, text):
    """Товара нет в КТРУ — «похожих» быть не должно (замечание 2 и 6 владельца).

    До правки поиск всегда возвращал пять категорий, и на «панамку» показывались
    «Нож для тимпанотомии», «Креативная панель» и прочее.
    """
    assert real_search.find(text) == []


@pytest.mark.parametrize("text,expected", [
    ("пальто муж 48", "Пальто"),
    ("шпа", "Шпажки"),
    ("шпажки", "Шпажки"),
    ("дерл", "Дрель ручная электрическая"),
    ("дрель 650 вт", "Дрель ручная электрическая"),
])
def test_significant_category_is_found(real_search, text, expected):
    """Нужная категория в подсказках, а лишние не добиваются до пяти."""
    names = [item.name for item in real_search.find(text)]
    assert any(expected in name for name in names)


def test_no_padding_when_only_two_categories_are_relevant(real_search):
    """«шпажки»: две подходящие категории — столько и показываем (замечание 4)."""
    names = [item.name for item in real_search.find("шпажки")]
    assert len(names) <= 3 and all("Шпажки" in name or "шпарильн" in name for name in names)


def test_short_query_prefers_word_prefix_over_inner_occurrence(real_search):
    """«шпа»: «шпажки» показываются, «шпагат» внутри длинного названия — не выше них (замечание 3)."""
    names = [item.name for item in real_search.find("шпа")]
    assert any("Шпажки" in name for name in names)
    shpazhki = next(index for index, name in enumerate(names) if "Шпажки" in name)
    shpagat = [index for index, name in enumerate(names) if "шпагат" in name.casefold()]
    assert all(index > shpazhki for index in shpagat)
