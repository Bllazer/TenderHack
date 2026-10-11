"""Тесты против хардкодинга: правила должны работать на НОВЫХ данных, а не на знакомых строках.

Три независимых способа поймать подгонку под тесты:

1. **Статическая проверка кода.** Из `app/*.py` берутся строковые литералы, которые
   реально участвуют в исполнении (docstring'и и комментарии исключаются через AST), и
   сравниваются со словами из запросов, встречающихся в тестах. Совпадение означает, что
   ответ мог быть «зашит» под конкретный ввод. Разрешены только слова-словари
   (названия характеристик, единицы), и каждое разрешение перечислено явно с причиной.
2. **Синтетический каталог.** Правила (префикс, опечатка, дедупликация, отказ,
   сокращение перечисления) проверяются на ВЫДУМАННЫХ названиях и значениях, которых
   нет ни в КТРУ, ни в тестах. Если поведение было зашито под реальный каталог,
   эти тесты упадут.
3. **Случайные запросы по реальному каталогу.** Входы генерируются из самого каталога
   с фиксированным зерном: префиксы, перестановки, пары несвязанных слов, имена с
   дублями. Ни один такой вход не встречается в коде и не мог быть предусмотрен заранее.
"""
from __future__ import annotations

import ast
import json
import os
import random
import re
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "tests"))

from app.catalog import AllowedValue, Catalog, Category, Position, Property, snapshot  # noqa: E402
from app.extraction import extract  # noqa: E402
from app.search import (Analyzer, E5Encoder, Search, SIGNIFICANCE, closest_vocabulary_token,  # noqa: E402
                        edit_distance, looks_like_typo)
from conftest import FixtureEncoder  # noqa: E402

INDEX_DIR = Path(os.environ.get("TEST_INDEX_DIR", "resources/catalog-build"))
MODEL_DIR = Path(os.environ.get("TEST_MODEL_DIR", "resources/model"))

#: Слова, которые в исполняемом коде допустимы: это языковой словарь предметной
#: области, а не ответы под конкретный запрос. Каждое разрешение — с причиной.
ALLOWED_LITERALS = {
    # analysis.py: до подтверждения категории глобальные прилагательные не считаются
    # именем характеристики; в список попадают только однозначные физические величины.
    "толщина", "ширина", "высота", "длина", "диаметр",
    # единицы измерения и краткие обозначения
    "мм", "см", "м", "кг", "вт", "в", "а", "шт",
    # checks.py: маркеры операторов сравнения в тексте («не менее», «не ниже»)
    "менее", "ниже", "более", "выше", "равно",
    # extraction.py: слово «размер» — метка системы размеров (EU/FR/RU) в designation-логике
    "размер",
    # extraction.py/structured.py: значения-заглушки «не знаю», «не задано»
    "знаю", "задано", "известно", "установлено", "данных",
}

WORD = re.compile(r"[а-яё]{4,}")


def executable_literals(path: Path) -> list[tuple[int, str]]:
    """Строковые литералы файла, участвующие в исполнении (без docstring'ов)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    return [(node.lineno, node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docstrings]


def words(text: str) -> set[str]:
    return set(WORD.findall(text.casefold().replace("ё", "е")))


#: Ключевые аргументы, значения которых — текст для пользователя, а не ответ.
MESSAGE_FIELDS = {"explanation", "suggested_action", "title", "reason", "label", "next_steps",
                  "message", "detail", "summary"}


def message_literals(tree: ast.AST) -> set[int]:
    """Идентификаторы строк, переданных как текст сообщения (title/explanation/…)."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg in MESSAGE_FIELDS:
            for inner in ast.walk(node.value):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    ids.add(id(inner.value))
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in MESSAGE_FIELDS:
                    for inner in ast.walk(node.value):
                        if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                            ids.add(id(inner.value))
    return ids


def looks_like_a_name(value: str) -> bool:
    """Короткий литерал-имя (1–3 слова) в отличие от предложения-сообщения."""
    stripped = value.strip()
    return bool(stripped) and len(stripped) <= 40 and len(stripped.split()) <= 3


def compared_literals(tree: ast.AST) -> list[tuple[int, str]]:
    """Строковые константы, с которыми код СРАВНИВАЕТ значение (==, in, набор вариантов).

    Именно так выглядит подгонка под конкретный ввод: ``if text == "панамка"``,
    ``if "шпажки" in category.name``, ``if value in {"шпа", "шпажки"}``. Тексты для
    пользователя (title/explanation/…) в сравнениях не участвуют, поэтому шума нет.
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        parts = [node.left, *node.comparators]
        for part in parts:
            for inner in ast.walk(part):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    found.append((inner.lineno, inner.value))
    return found


def test_no_user_input_is_compared_to_a_query_literal():
    """Ввод не сравнивается со строковыми константами-запросами.

    Проверяется структурно: если правило реализовано честно, решение принимается по
    порогам, расстоянию, размерности единицы и словарю каталога — а не по списку
    знакомых фраз. Разрешён только словарь предметной области (`ALLOWED_LITERALS`).
    """
    query_words: set[str] = set()
    for path in (BACKEND / "tests").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for value in re.findall(r'description"?\s*[:=]\s*"([^"]+)"', text):
            query_words |= words(value)
        for value in re.findall(r'"([^"]{6,})"', text):
            if " " in value and re.search(r"[а-яё]", value, re.I):
                query_words |= words(value)
    assert query_words, "не удалось собрать слова запросов из тестов"

    offenders: list[str] = []
    for path in sorted((BACKEND / "app").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for line, value in compared_literals(tree):
            if not looks_like_a_name(value):
                continue
            for word in words(value):
                if word in query_words and word not in ALLOWED_LITERALS:
                    offenders.append(f"{path.name}:{line}: сравнение с {value[:60]!r} («{word}»)")
    assert not offenders, "ввод сравнивается со словами из тестовых запросов:\n" + "\n".join(offenders)


def catalogue_names() -> set[str]:
    """Названия категорий из фикстуры и (если есть) из реального снимка."""
    names: set[str] = set()
    fixture = BACKEND / "tests" / "fixtures" / "verified.json"
    if fixture.exists():
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        names |= {category["name"].casefold().replace("ё", "е") for category in payload.get("categories", [])}
        names |= {position["name"].casefold().replace("ё", "е") for position in payload.get("positions", [])}
    if (INDEX_DIR / "manifest.json").exists():
        from app.catalog import load_snapshot
        snapshot_ = load_snapshot(INDEX_DIR)
        names |= {category.name.casefold().replace("ё", "е") for category in snapshot_.catalog.categories}
    return names


def test_no_catalogue_name_is_a_literal_in_code():
    """Названия категорий и позиций каталога не должны встречаться в коде как строки.

    Второй, независимый признак подгонки: таблица «название товара → ответ». Названия
    берутся из самого каталога (фикстура всегда, реальный снимок — если собран), а код
    их знать не должен: он обязан работать по словарю, который ему передали.
    """
    names = catalogue_names()
    assert len(names) > 5, f"не удалось собрать названия каталога: {len(names)}"

    offenders: list[str] = []
    for path in sorted((BACKEND / "app").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = message_literals(tree)
        for line, value in executable_literals(path):
            if id(value) in skip:
                continue
            if value.casefold().replace("ё", "е") in names:
                offenders.append(f"{path.name}:{line}: название из каталога {value!r}")
    assert not offenders, "названия каталога зашиты в код:\n" + "\n".join(offenders)


# --------------------------------------------------------- синтетический каталог
def invented_catalog() -> Catalog:
    """Каталог из выдуманных слов: их нет ни в КТРУ, ни в тестах, ни в коде."""
    def number(identifier, name, unit="мм"):
        allowed = (AllowedValue(kind="interval", lower="0", upper="1000", unit=unit,
                                raw_unit=unit, source="test"),)
        return Property(id=identifier, name=name, value_type="number", required=False,
                        verified=True, evidence="test", allowed=allowed)

    def enum(identifier, name, values):
        return Property(id=identifier, name=name, value_type="text", required=True, verified=True,
                        evidence="test", allowed=tuple(AllowedValue(kind="enum", value=value, source="test")
                                                       for value in values))

    categories = [Category(id="zumba", name="Зумбатрон бытовой", search_text="зумбатрон бытовой",
                           position_codes=("Z1",)),
                  Category(id="zumba-lab", name="Зумбатрон лабораторный",
                           search_text="зумбатрон лабораторный", position_codes=("Z2",)),
                  Category(id="krivlyak", name="Кривляк опорный", search_text="кривляк опорный",
                           position_codes=("K1",)),
                  Category(id="krivlyak-two", name="Кривляк опорный", search_text="кривляк опорный",
                           position_codes=("K2",))]
    positions = [Position(code="Z1", name="Зумбатрон бытовой", source_path="test", status="actual",
                          is_template=False, properties=(number("mass", "Масса"), enum("mode", "Режим", ("Зумбовый", "Кривой")))),
                 Position(code="Z2", name="Зумбатрон лабораторный", source_path="test", status="actual",
                          is_template=False, properties=(number("mass", "Масса"),)),
                 Position(code="K1", name="Кривляк опорный", source_path="test", status="actual",
                          is_template=False, properties=(enum("mode", "Режим", ("Зумбовый", "Кривой")),)),
                 Position(code="K2", name="Кривляк опорный", source_path="test",
                          status="actual", is_template=False, properties=(enum("mode", "Режим", ("Зумбовый",)),))]
    return Catalog(format_version=1, catalog_version="invented", source_description="test",
                   validation_report="test", synthetic=True, categories=categories, positions=positions)


def invented_search() -> Search:
    catalog = invented_catalog()
    analyzer = Analyzer()
    vectors = FixtureEncoder().encode([category.search_text for category in catalog.categories], "passage: ")
    data = snapshot(catalog, vectors=vectors,
                    tokens=tuple(analyzer.tokens(category.search_text) for category in catalog.categories))
    # Порог значимости отключён: у стенда вместо модели заглушка, оценки структурно ниже.
    return Search(data, FixtureEncoder(), analyzer)


def test_prefix_rule_works_on_invented_names():
    """«зумб» находит «Зумбатрон …» — правило префикса не знает реальных названий."""
    found = invented_search().find("зумб")
    assert [item.name for item in found] == ["Зумбатрон бытовой", "Зумбатрон лабораторный"]


def test_typo_rule_works_on_invented_names():
    """Перестановка в выдуманном слове исправляется: «зумбаторн» → «зумбатрон»."""
    vocabulary = ("зумбатрон", "бытовой", "лабораторный", "кривляк", "опорный")
    assert closest_vocabulary_token("зумбаторн", vocabulary) == "зумбатрон"
    found = invented_search().find("зумбаторн")
    assert any("Зумбатрон" in item.name for item in found)


def test_missing_product_returns_nothing_in_invented_catalog():
    """«крокозябра» нет в выдуманном каталоге — выдача пустая, как и для «панамки»."""
    assert invented_search().find("крокозябра") == []


def test_duplicate_names_are_shown_once_in_invented_catalog():
    """Два одноимённых семейства в выдаче дают одну строку (правило дедупликации)."""
    names = [item.name for item in invented_search().find("кривляк опорный")]
    assert names.count("Кривляк опорный") == 1


def test_abbreviation_rule_works_on_invented_values():
    """«зумб» → «Зумбовый»: сокращение работает на выдуманном перечислении."""
    scope = invented_catalog().positions[0].properties
    facts, _ = extract("зумбатрон зумб 12", tuple(prop.name for prop in scope), scoped_properties=scope)
    assert any(fact.name == "Режим" and fact.value == "Зумбовый" for fact in facts)


def test_abbreviation_needs_a_single_candidate():
    """Если сокращение подходит двум значениям одного свойства, факт не выдумывается."""
    property_ = Property(id="mode", name="Режим", value_type="text", required=True, verified=True,
                         evidence="test",
                         allowed=(AllowedValue(kind="enum", value="Красный", source="test"),
                                 AllowedValue(kind="enum", value="Красноватый", source="test")))
    facts, _ = extract("прибор крас 12", ("Режим",), scoped_properties=(property_,))
    assert not any(fact.name == "Режим" for fact in facts)


def test_thresholds_are_not_tied_to_specific_queries():
    """Ключевые константы — числа, а не списки знакомых слов.

    Тест фиксирует, что решения принимаются по величинам: если кто-то заменит порог
    таблицей конкретных запросов, тест упадёт.
    """
    from app import search as search_module
    assert isinstance(search_module.SIGNIFICANCE, float)
    assert 0.0 < search_module.SIGNIFICANCE < 1.0
    source = Path(search_module.__file__).read_text(encoding="utf-8")
    assert "SIGNIFICANCE = {" not in source and "SIGNIFICANCE = [" not in source


# ------------------------------------------------- реальный каталог, случайные входы
@pytest.fixture(scope="module")
def real_search() -> Search:
    if os.environ.get("RUN_E5_TESTS") != "1":
        pytest.skip("нужны реальный индекс и веса e5 (RUN_E5_TESTS=1)")
    if not (INDEX_DIR / "manifest.json").exists() or not (MODEL_DIR / "model.meta.json").exists():
        pytest.skip(f"нет артефактов: {INDEX_DIR} и/или {MODEL_DIR}")
    metadata = json.loads((MODEL_DIR / "model.meta.json").read_text(encoding="utf-8"))
    from app.catalog import load_snapshot
    return Search(load_snapshot(INDEX_DIR), E5Encoder(MODEL_DIR, metadata["revision"]), Analyzer())


def random_categories(search: Search, count: int, seed: int) -> list:
    rng = random.Random(seed)
    names = [category for category in search.data.catalog.categories
             if len(category.name.split()) == 1 and len(category.name) >= 6]
    return rng.sample(names, min(count, len(names)))


@pytest.mark.parametrize("seed", [11, 23, 37])
def test_random_word_prefix_finds_its_category(real_search, seed):
    """Случайное слово каталога, взятое префиксом в 4 буквы, приводит к своим семействам.

    Обобщение замечания «шпа» → «шпажки»: правило не знает конкретных слов. Проверка
    идёт на словах, которые исправление опечаток НЕ заменяет другим словом: короткий
    префикс иногда сам оказывается опечаткой частого слова («тамб» → «тумба»), и тогда
    канал исправления законно выводит вперёд другое семейство. Полное совпадение
    требуется, когда под префикс подходит не больше семи семейств.
    """
    names = [category.name.casefold().replace("ё", "е") for category in real_search.data.catalog.categories]
    misses = []
    checked = 0
    for category in random_categories(real_search, 16, seed):
        prefix = category.name.casefold().replace("ё", "е")[:4]
        if len(prefix) < 4:
            continue
        if set(real_search.retrieval_variants_for(prefix)) - {prefix}:
            continue                                    # префикс исправляется в другое слово
        competitors = [name for name in names if name.split() and name.split()[0].startswith(prefix)]
        found = real_search.find(prefix)
        checked += 1
        if not any(item.name.casefold().split() and item.name.casefold().split()[0].startswith(prefix)
                   for item in found):
            misses.append((prefix, category.name, len(competitors), [item.name for item in found][:3]))
        elif len(competitors) <= 7 and not any(category.name.casefold()[:6] in item.name.casefold()
                                              for item in found):
            misses.append((prefix, category.name, len(competitors), [item.name for item in found][:3]))
    assert checked >= 4, f"слишком мало проверенных префиксов: {checked}"
    assert not misses, f"префикс не дал своих семейств: {misses}"


@pytest.mark.parametrize("seed", [5, 17, 29])
def test_random_transposition_is_corrected(real_search, seed):
    """Перестановка соседних букв в случайном слове каталога исправляется, мусора нет.

    Обобщение замечания «дерл» → «дрель». Перестановка берётся не в первой букве:
    исправление намеренно требует ту же первую букву, потому что иначе под кандидата
    подходят слова с другим корнем. Допускается редкая неоднозначность (мутация
    совпала с другим реальным словом), но ответ обязан быть близким к вводу.
    """
    rng = random.Random(seed)
    words = sorted({word for category in real_search.data.catalog.categories
                    for word in category.name.casefold().replace("ё", "е").split()
                    if word.isalpha() and len(word) >= 6})
    assert words, "нет слов каталога для проверки"
    recovered, garbage = 0, []
    for word in rng.sample(words, min(25, len(words))):
        position = rng.randrange(1, len(word) - 1)          # первая буква остаётся на месте
        broken = word[:position] + word[position + 1] + word[position] + word[position + 2:]
        fixed = closest_vocabulary_token(broken, tuple(words))
        if fixed == word:
            recovered += 1
        elif fixed is not None and edit_distance(broken, fixed, 3) > 2:
            garbage.append((broken, word, fixed))
    assert not garbage, f"исправление вернуло далёкое слово: {garbage[:5]}"
    assert recovered >= 20, f"перестановки исправляются реже ожидаемого: {recovered} из 25"


@pytest.mark.parametrize("seed", [2, 13, 41])
def test_random_unrelated_words_are_not_typos(real_search, seed):
    """Случайные несвязанные слова каталога не считаются опечатками друг друга."""
    rng = random.Random(seed)
    words = sorted({word for category in real_search.data.catalog.categories
                    for word in category.name.casefold().replace("ё", "е").split()
                    if word.isalpha() and len(word) >= 5})
    pairs = [(rng.choice(words), rng.choice(words)) for _ in range(200)]
    wrong = [(left, right) for left, right in pairs
             if left[0] != right[0] and edit_distance(left, right, 3) > 2 and looks_like_typo(left, right)]
    assert not wrong, f"несвязанные слова считаются опечатками: {wrong[:5]}"


@pytest.mark.parametrize("seed", [3, 19])
def test_random_duplicate_names_are_listed_once(real_search, seed):
    """Для случайных имён с дублями в каталоге выдача не содержит повторов."""
    from collections import Counter
    counter = Counter(category.name for category in real_search.data.catalog.categories)
    duplicated = [name for name, count in counter.items() if count > 1 and len(name) >= 6]
    rng = random.Random(seed)
    checked = 0
    for name in rng.sample(duplicated, min(10, len(duplicated))):
        found = [item.name for item in real_search.find(name)]
        assert len(found) == len(set(found)), f"дубли в выдаче на {name!r}: {found}"
        checked += 1
    assert checked, "не нашлось имён с дублями"


@pytest.mark.parametrize("seed", [7, 31])
def test_every_candidate_is_justified_by_words_or_score(real_search, seed):
    """В выдаче нет «добивки»: каждый кандидат либо словесно связан, либо прошёл порог.

    Инвариант ловит и искусственное заполнение списка до пяти, и подмешанные ответы:
    посторонний кандидат без словесной связи и с оценкой ниже порога недопустим.
    Связь проверяется и по вариантам запроса (исправление опечаток/раскладки): если
    слово исправлено, кандидат обязан быть связан с исправленным словом, а не с чем-то
    третьим.
    """
    rng = random.Random(seed)
    by_id = {category.id: index for index, category in enumerate(real_search.data.catalog.categories)}
    tokens_pool = [category.name.casefold().split()[0] for category in real_search.data.catalog.categories
                   if category.name.split()]
    assert len(tokens_pool) > 100
    for query in rng.sample(tokens_pool, 10):
        variants = real_search.retrieval_variants_for(query)
        tokens = {token for word in (query, *variants) for token in word.casefold().replace("ё", "е").split()}
        for item in real_search.find(query):
            linked = real_search.relevant(by_id[item.id], tokens)
            assert linked or item.rank_score >= SIGNIFICANCE, (query, item.name, item.rank_score)
            assert item.rank <= 7
