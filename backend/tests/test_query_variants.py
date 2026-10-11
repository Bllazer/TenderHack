"""Запросы с числами, опечатками, их комбинацией и разным порядком слов.

Четыре категории из задачи:

1. **числа** — «молоток 20 см», «дрель 650 Вт»: число и единица не должны мешать
   найти тип товара;
2. **опечатки** — «малоток», «дрелль», «кресло офесное»: канал char n-gram
   вытягивает написание, которого нет в каталоге;
3. **числа и опечатки** — «дрелль 650 вт», «шуруповирт 18в»: обе трудности сразу;
4. **порядок написания** — «молоток 20см» и «20см молоток» должны давать один и
   тот же результат.

Файл из двух частей:

* быстрая — извлечение фактов (`app.extraction`) и подготовка запроса
  (`query_variants`, `CharNgramIndex`): без весов и индекса, идёт всегда;
* на реальном индексе — `Search.find` с настоящей моделью e5: включается
  `RUN_E5_TESTS=1`, потому что нужны `resources/catalog-build` и `resources/model`.

Ожидаемые значения — не догадки: они сняты прогоном на индексе
`catalog_version=clean5-578e29f99d1a803c` (14 550 семей) и модели ревизии
`614241f622f53c4eeff9890bdc4f31cfecc418b3`. Измеренные промахи помечены
`xfail` с фактическим ответом, чтобы не выдавать их за успех.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.catalog import AllowedValue, Property
from app.extraction import extract
from app.search import Analyzer, CharNgramIndex, E5Encoder, Search, query_variants, retrieval_variants

#: имена характеристик для извлечения — как их видит стадия A из каталога
PROPERTY_NAMES = ("Мощность", "Напряжение", "Высота", "Ширина", "Длина", "Масса",
                  "Объём", "Диаметр", "Толщина", "Глубина", "Вес", "Температура")

INDEX_DIR = Path(os.environ.get("TEST_INDEX_DIR", "resources/catalog-build"))
MODEL_DIR = Path(os.environ.get("TEST_MODEL_DIR", "resources/model"))


def facts_of(text: str):
    return extract(text, PROPERTY_NAMES)


# ===================================================== 1. числа (извлечение) ==
@pytest.mark.parametrize("text,name,value,unit", [
    ("Мощность 650 Вт", "Мощность", "650", "Вт"),
    ("Мощность: 650 Вт", "Мощность", "650", "Вт"),
    ("мощность 650вт", "Мощность", "650", "вт"),          # без пробела перед единицей
    ("Мощность 0,65 кВт", "Мощность", "0,65", "кВт"),     # десятичная запятая
    ("Высота 2000 мм", "Высота", "2000", "мм"),
    ("Длина 20 см", "Длина", "20", "см"),
    ("Масса 2,5 кг", "Масса", "2,5", "кг"),
    ("Температура −40 °C", "Температура", "−40", "°C"),   # типографский минус
])
def test_number_after_the_name_is_extracted(text, name, value, unit):
    facts, fragments = facts_of(text)
    assert fragments == []
    assert [(fact.name, fact.value, fact.unit) for fact in facts] == [(name, value, unit)]


@pytest.mark.parametrize("forward,backward,name,value,unit", [
    ("Мощность 650 Вт", "650 Вт мощность", "Мощность", "650", "Вт"),
    ("Высота 2000 мм", "2000 мм высота", "Высота", "2000", "мм"),
    ("Длина 20 см", "20 см длина", "Длина", "20", "см"),
    ("Масса 2,5 кг", "2,5 кг масса", "Масса", "2,5", "кг"),
    ("Напряжение 220 В", "220 В напряжение", "Напряжение", "220", "В"),
])
def test_both_word_orders_give_the_same_fact(forward, backward, name, value, unit):
    """«650 Вт мощность» разбирается так же, как «Мощность 650 Вт».

    Раньше порядок «значение → имя» терялся целиком: факт не доходил ни до C, ни
    до вопроса D, а строка оставалась неразобранным фрагментом.
    """
    for text in (forward, backward):
        facts, fragments = facts_of(text)
        assert fragments == [], text
        assert [(fact.name, fact.value, fact.unit) for fact in facts] == [(name, value, unit)], text


def test_value_first_with_a_colon_is_swapped():
    """«650 Вт: мощность» — значение слева, имя справа."""
    facts, fragments = facts_of("650 Вт: мощность")
    assert fragments == []
    assert [(fact.name, fact.value, fact.unit) for fact in facts] == [("Мощность", "650", "Вт")]


def test_value_first_composite_does_not_produce_a_garbage_value():
    """«650 Вт мощность, 220 В напряжение» — два факта, а не мусорное значение.

    До правки разбор шёл по порядку словаря: «650 Вт» доставалось напряжению, а
    «, 220 В напряжение» становилось значением мощности. Порядок разбора теперь
    текстовый, поэтому значение принадлежит тому имени, рядом с которым стоит.
    """
    facts, fragments = facts_of("650 Вт мощность, 220 В напряжение")
    assert fragments == []
    assert [(fact.name, fact.value, fact.unit) for fact in facts] == [
        ("Мощность", "650", "Вт"), ("Напряжение", "220", "В")]


@pytest.mark.parametrize("text,expected", [
    ("Мощность 650 Вт напряжение 220 В", [("Мощность", "650", "Вт"), ("Напряжение", "220", "В")]),
    ("650 Вт мощность 220 В напряжение", [("Мощность", "650", "Вт"), ("Напряжение", "220", "В")]),
    ("2000 мм высота, 600 мм ширина", [("Высота", "2000", "мм"), ("Ширина", "600", "мм")]),
    ("Высота 2000 мм, ширина 600 мм", [("Высота", "2000", "мм"), ("Ширина", "600", "мм")]),
])
def test_two_facts_in_any_order_keep_their_own_values(text, expected):
    """Составная строка: каждое значение остаётся при своём имени в любом порядке."""
    facts, fragments = facts_of(text)
    assert fragments == [], text
    assert [(fact.name, fact.value, fact.unit) for fact in facts] == expected, text


def test_tail_that_starts_a_new_property_is_not_a_value():
    """«Мощность напряжение 220 В»: чужой текст не становится значением мощности."""
    facts, _ = facts_of("Мощность напряжение 220 В")
    assert [(fact.name, fact.value, fact.unit) for fact in facts] == [("Напряжение", "220", "В")]


# ================================================== 2. опечатки (извлечение) ==
@pytest.mark.parametrize("text,name,value,unit", [
    ("Мошность 650 Вт", "Мощность", "650", "Вт"),
    ("Мощьность 650 Вт", "Мощность", "650", "Вт"),
    ("напряжене 220 В", "Напряжение", "220", "В"),
    ("Мошность: 650 Вт", "Мощность", "650", "Вт"),
])
def test_confident_typo_is_replaced_automatically(text, name, value, unit):
    """Уверенная опечатка (≥ 0.85, кандидат один, ≥ 5 букв) заменяется сразу.

    Критерий выбран по замерам: на 774 однобуквенных опечатках похожесть на истинное
    имя имеет медиану 0.900, выше 0.85 попадает 80 % опечаток, и ни одной спорной
    (чужое имя тоже ≥ порога); максимальная похожесть на чужое — 0.833.
    Исходное написание при этом не теряется: оно остаётся в `raw`.
    """
    facts, fragments = facts_of(text)
    assert fragments == []
    assert [(fact.name, fact.value, fact.unit) for fact in facts] == [(name, value, unit)]
    assert facts[0].raw.replace(":", " ").split() == text.replace(":", " ").split()


@pytest.mark.parametrize("text,surface,value,unit", [
    ("Высата 2000 мм", "Высата", "2000", "мм"),
    ("маса 2,5 кг", "маса", "2,5", "кг"),
    ("Шырина 600 мм", "Шырина", "600", "мм"),
])
def test_unsure_typo_keeps_the_written_name_for_the_user_to_fix(text, surface, value, unit):
    """Неуверенная опечатка (0.72…0.85 или короткое слово) не подменяется.

    Факт уходит с написанием пользователя, а поток коррекций спрашивает
    «Возможно, вы имели в виду …» — см.
    `test_correction_http.py::test_unsure_typo_is_offered_for_confirmation`.
    """
    facts, fragments = facts_of(text)
    assert fragments == []
    assert [(fact.name, fact.value, fact.unit) for fact in facts] == [(surface, value, unit)]


def test_typo_text_value_is_not_invented_without_confirmation():
    """Без подтверждения имени текстовое значение не выдумывается.

    «Высата Полиэстер» — имя в уровне подсказки, значение не измерение: факт не
    создаётся, текст остаётся фрагментом. С измерением факт создаётся (см. выше):
    там значение однозначно, и пользователю есть что подтверждать.
    """
    facts, fragments = facts_of("Высата Полиэстер")
    assert facts == []
    assert [fragment.raw for fragment in fragments] == ["Высата Полиэстер"]


@pytest.mark.parametrize("text,unit", [
    ("мощность 650 вТТ", "вТТ"),
    ("Длина 20 сМ", "сМ"),
])
def test_typo_in_a_unit_keeps_the_surface_spelling(text, unit):
    """Единицу проверяет C, а не извлечение: написание сохраняется как в тексте."""
    facts, fragments = facts_of(text)
    assert fragments == []
    assert facts[0].unit == unit
    assert facts[0].raw == text


# ============================== 3. числа и опечатки (извлечение и подготовка) ==
@pytest.mark.parametrize("text,name,value,unit", [
    ("дрелль 650 вт", "Мощность", "650", "вт"),
    ("малоток 20см", "Длина", "20", "см"),
    ("шуруповирт 18в", "Напряжение", "18", "в"),
])
def test_product_name_typo_with_a_number_is_not_a_property_fact(text, name, value, unit):
    """Название товара — не характеристика, но число рядом с ним — значение.

    Имя товара с опечаткой (и без неё) характеристикой не становится: «дрелль» так
    и остаётся неразобранным фрагментом. А измерение привязывается по размерности,
    когда область подтверждена и характеристика такой размерности в ней одна —
    правило владельца «однозначно по единице измерения — расписать» (см.
    `docs/MODEL_RULES.md`, правило 2).
    """
    scope = (Property(id="p1", name=name, value_type="number", required=False, verified=True,
                      evidence="test", allowed=(AllowedValue(kind="interval", lower="0", upper="100000",
                                                             unit=unit, raw_unit=unit, source="test"),)),)
    facts, fragments = extract(text, PROPERTY_NAMES, scoped_properties=scope)
    assert [(fact.name, fact.value, fact.unit) for fact in facts] == [(name, value, unit)]
    assert any(fragment.raw.strip() in text for fragment in fragments)


def test_query_variants_fix_layout_but_not_cyrillic_typos():
    """Варианты запроса лечат раскладку и гомоглифы, а не опечатки.

    Опечатки — работа канала char n-gram, поэтому здесь важно зафиксировать
    границу: «малоток» вариантами не исправляется.
    """
    assert query_variants("малоток 20 см") == ("малоток 20 см",)
    assert query_variants("дрелль 650 вт") == ("дрелль 650 вт",)
    assert query_variants("lhtkm") == ("lhtkm", "дрель")


#: слова названий категорий и леммы каталога — два разных словаря (см. retrieval_variants)
NAME_WORDS = frozenset({"шкаф", "документов", "для", "мебель", "хранения", "бумаг", "замком",
                        "молоток", "шуруповерт", "аккумуляторный"})
LEMMAS = NAME_WORDS | {"документ", "хранить", "бумага", "замок", "сверление", "отверстие",
                       "розетка", "электроинструмент"}


@pytest.mark.parametrize("text,expected", [
    ("молоток 20 см", ()),                                  # осталось одно слово — вариант не нужен
    ("шкаф для документов 2000 мм", ("шкаф для документов",)),
    ("мебель для хранения бумаг с замком", ()),             # «с» — предлог, не секунда
    ("шуруповирт аккумуляторный", ("шуруповерт аккумуляторный",)),
    ("электроинструмент для сверления отверстий от розетки", ()),   # парафраз не «правим»
])
def test_retrieval_variants(text, expected):
    """Варианты для поиска типа: без измерений после числа и с исправленной опечаткой.

    Три правила, каждое из замера:

    * единица считается измерением только ПОСЛЕ числа — иначе предлог «с» (он же
      секунда) выпадал из «мебель … с замком»;
    * «слово известно» проверяется по ЛЕММАМ каталога — падежная форма не опечатка,
      иначе законные «сверления»/«розетки» заменялись бы мусором;
    * подстановка берётся из слов НАЗВАНИЙ — в запрос попадает естественное
      «документов», а не лемма «документ».
    """
    assert retrieval_variants(text, NAME_WORDS, None, LEMMAS) == expected


def test_char_channel_recovers_a_cyrillic_typo_with_a_number():
    """Опечатка в названии вытягивается символьными n-граммами, число не мешает."""
    names = ["Молоток", "Дрель ручная электрическая", "Кресло офисное",
             "Рюкзак туристический", "Шуруповерт"]
    index = CharNgramIndex(names)
    assert index.scores("малоток 20 см").argmax() == 0
    assert index.scores("дрелль ручная электрическая").argmax() == 1
    assert index.scores("кресло офесное").argmax() == 2


# ================================= 4. поиск на реальном индексе (модель B) ====
@pytest.fixture(scope="module")
def search() -> Search:
    if os.environ.get("RUN_E5_TESTS") != "1":
        pytest.skip("нужны реальный индекс и веса e5 (RUN_E5_TESTS=1)")
    if not (INDEX_DIR / "manifest.json").exists() or not (MODEL_DIR / "model.meta.json").exists():
        pytest.skip(f"нет артефактов: {INDEX_DIR} и/или {MODEL_DIR}")
    metadata = json.loads((MODEL_DIR / "model.meta.json").read_text(encoding="utf-8"))
    from app.catalog import load_snapshot
    data = load_snapshot(INDEX_DIR)
    return Search(data, E5Encoder(MODEL_DIR, metadata["revision"]), Analyzer())


def top1(search: Search, query: str, expected_id: str):
    found = search.find(query)
    assert found, query
    return found[0]


@pytest.mark.parametrize("query,expected_id,expected_name", [
    ("молоток 20 см", "25.73.30.141-00000011", "Молоток"),
    ("дрель 650 Вт", "28.24.11.000-00000003", "Дрель ручная электрическая"),
    ("дрель ручная электрическая 650 вт", "28.24.11.000-00000003", "Дрель ручная электрическая"),
    ("шуруповерт 18 В", "28.24.11.000-00000032", "Шуруповерт"),
    ("кресло офисное 120 кг", "31.01.12.160-00000006", "Кресло офисное"),
    ("рюкзак туристический 30 л", "15.12.12.192-00000002", "Рюкзак туристический"),
    ("шкаф для документов 2000 мм", "31.01.12.139-00000002", "Шкаф деревянный для документов"),
    ("перчатки нитриловые неопудренные размер M", "22.19.60.111-00000002",
     "Перчатки хирургические нитриловые, неопудренные"),
])
def test_numbers_do_not_break_the_lookup(search, query, expected_id, expected_name):
    """Число и единица в запросе не должны уводить от нужного семейства."""
    best = top1(search, query, expected_id)
    assert best.id == expected_id, f"{query!r} -> {best.name!r}"
    assert best.name == expected_name


@pytest.mark.parametrize("query,expected_id,expected_name", [
    ("малоток 20 см", "25.73.30.141-00000011", "Молоток"),
    ("дрелль ручная электрическая", "28.24.11.000-00000003", "Дрель ручная электрическая"),
    ("кресло офесное", "31.01.12.160-00000006", "Кресло офисное"),
    ("рюкзак турестический", "15.12.12.192-00000002", "Рюкзак туристический"),
    ("шкаф для дакументов", "31.01.12.139-00000002", "Шкаф деревянный для документов"),
    ("электродрель от сити", "28.24.11.000-00000003", "Дрель ручная электрическая"),
])
def test_typos_are_recovered(search, query, expected_id, expected_name):
    best = top1(search, query, expected_id)
    assert best.id == expected_id, f"{query!r} -> {best.name!r}"
    assert best.name == expected_name


def test_typo_in_the_product_name_keeps_the_family(search):
    """«перчятки» даёт то же семейство перчаток, хотя код другой.

    Здесь проверяется семейство, а не точный код: у «перчаток» несколько
    равноправных позиций, и выбор между ними — дело C, а не поиска.
    """
    best = top1(search, "перчятки нитриловые", "")
    assert best.name.startswith("Перчатки")


def test_typo_with_a_word_shared_by_another_family_is_resolved(search):
    """«шуруповирт аккумуляторный» → «Шуруповерт».

    Раньше опечатка в первой половине была слабее точного совпадения второй
    («аккумуляторный» тянул к «Батарее аккумуляторной»). Вариант запроса с
    исправленной опечаткой добавляет нужную семью, не подменяя исходный запрос.
    """
    best = top1(search, "шуруповирт аккумуляторный", "")
    assert best.name == "Шуруповерт"


@pytest.mark.parametrize("query,expected_id", [
    ("дрелль 650 вт", "28.24.11.000-00000003"),
    ("шуруповирт 18в", "28.24.11.000-00000032"),
    ("кресло офесное 120 кг", "31.01.12.160-00000006"),
    ("рюкзак турестический 30 л", "15.12.12.192-00000002"),
    ("малоток 20см", "25.73.30.141-00000011"),
])
def test_numbers_with_typos_are_recovered(search, query, expected_id):
    """Опечатка и число одновременно — обе трудности снимаются вместе."""
    best = top1(search, query, expected_id)
    assert best.id == expected_id, f"{query!r} -> {best.name!r}"


def test_number_with_a_typo_that_drifts_to_another_family_is_resolved(search):
    """«шкаф для дакументов 2000 мм» → «Шкаф деревянный для документов».

    Без числа тот же запрос находил верно, с числом уезжал в «Шкаф для одежды»:
    числа в поисковую запись каталога не входят. Вариант без измерений возвращает
    типу товара его вес в запросе.
    """
    best = top1(search, "шкаф для дакументов 2000 мм", "")
    assert best.id == "31.01.12.139-00000002"


@pytest.mark.parametrize("first,second", [
    ("молоток 20 см", "20 см молоток"),
    ("дрель ручная электрическая 650 вт", "650 вт дрель ручная электрическая"),
    ("кресло офисное 120 кг", "120 кг кресло офисное"),
    ("шуруповерт аккумуляторный 18 в", "18 в шуруповерт аккумуляторный"),
    ("рюкзак туристический 30 л", "30 л рюкзак туристический"),
    ("шкаф для документов 2000 мм", "2000 мм шкаф для документов"),
    ("дрель электрическая ручная", "ручная электрическая дрель"),
    ("кресло офисное с подлокотниками", "с подлокотниками кресло офисное"),
    ("перчатки нитриловые неопудренные", "неопудренные нитриловые перчатки"),
])
def test_word_order_does_not_change_the_result(search, first, second):
    """«молоток 20см» и «20см молоток» — один и тот же ответ.

    Порядок слов не должен влиять на результат: поиск сравнивает множества
    лемм и n-грамм, а не последовательность.
    """
    left, right = search.find(first), search.find(second)
    assert left and right, (first, second)
    assert left[0].id == right[0].id, f"{first!r} -> {left[0].name!r}, {second!r} -> {right[0].name!r}"


@pytest.mark.parametrize("first,second,expected_id", [
    ("молоток 20 см", "20 см молоток", "25.73.30.141-00000011"),
    ("дрель ручная электрическая 650 вт", "650 вт дрель ручная электрическая",
     "28.24.11.000-00000003"),
    ("кресло офисное 120 кг", "120 кг кресло офисное", "31.01.12.160-00000006"),
    ("шкаф для документов 2000 мм", "2000 мм шкаф для документов", "31.01.12.139-00000002"),
])
def test_word_order_keeps_the_expected_family(search, first, second, expected_id):
    """Совпадение порядка подтверждается ещё и ожидаемым семейством."""
    for query in (first, second):
        assert top1(search, query, expected_id).id == expected_id, query
