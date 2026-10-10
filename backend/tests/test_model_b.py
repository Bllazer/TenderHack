"""Модель B — сопоставление описания с КТРУ (`docs/MODEL.md` §4.2).

Три канала (BM25, e5, char n-gram), объединение по рангам (RRF) и контракт
выдачи. Тесты с настоящими весами e5 включаются `RUN_E5_TESTS=1`; остальные
работают на синтетическом энкодере и не требуют ни модели, ни индекса.

Никаких порогов «качества» здесь нет: у организаторов нет разметки, поэтому
метрики модели B измеряются стендом `C:\\hack\\bench_search.py`, а тесты
фиксируют поведение и инварианты.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest

from app.catalog import Catalog, Category, snapshot
from app.search import Analyzer, CharNgramIndex, E5Encoder, Search
from conftest import FixtureEncoder


def test_char_channel_ranks_a_typo_next_to_the_exact_name():
    """Канал char n-gram — страховка от опечаток; BM25 на леммах их не видит."""
    names = ["Дрель ручная электрическая", "Шуруповерт аккумуляторный",
             "Куртка рабочая утеплённая", "Кресло офисное"]
    index = CharNgramIndex(names)
    for typo in ("дрелль ручная электрическая", "дрель ручная электическая"):
        assert index.scores(typo).argmax() == 0, typo


def test_find_returns_at_most_five_candidates_and_no_score_threshold():
    """Известное свойство модели B: порога значимости нет, поэтому на «чужой»
    запрос всё равно приходят пять категорий (`docs/ML_STATE.md` §10.2)."""
    categories = tuple(
        Category(id=f"c{index}", name=f"Категория {index}", search_text=f"категория {index}",
                 position_codes=(), link_verified=True, coverage_verified=True, evidence="test")
        for index in range(6))
    catalog = Catalog(catalog_version="test", source_description="test",
                      validation_report="test", synthetic=True, categories=categories)
    analyzer = Analyzer()
    data = snapshot(catalog,
                    vectors=FixtureEncoder().encode([item.search_text for item in categories], "passage: "),
                    tokens=tuple(analyzer.tokens(item.search_text) for item in categories))
    search = Search(data, FixtureEncoder(), analyzer)

    found = search.find("совершенно посторонний запрос")
    assert len(found) == 5
    assert [item.rank for item in found] == [1, 2, 3, 4, 5]
    assert all(item.id.startswith("c") for item in found)


def test_find_keeps_the_original_query_and_adds_variants():
    """Варианты раскладки/гомоглифов могут добавить попадание, но не убрать его."""
    categories = tuple(
        Category(id=f"c{index}", name=name, search_text=name, position_codes=(),
                 link_verified=True, coverage_verified=True, evidence="test")
        for index, name in enumerate(["Дрель ручная электрическая", "Кресло офисное"]))
    catalog = Catalog(catalog_version="test", source_description="test",
                      validation_report="test", synthetic=True, categories=categories)
    analyzer = Analyzer()
    data = snapshot(catalog, vectors=FixtureEncoder().encode(
        [item.search_text for item in categories], "passage: "),
        tokens=tuple(analyzer.tokens(item.search_text) for item in categories))
    search = Search(data, FixtureEncoder(), analyzer)

    # «lhtkm» — «дрель» в неверной раскладке: исходный запрос не подменяется,
    # вариант добавляется, поэтому категория находится.
    assert search.find("lhtkm")[0].id == "c0"
    assert search.find("кресло офисное")[0].id == "c1"


def model_directory() -> Path:
    return Path(os.environ.get("TEST_MODEL_DIR", "resources/model"))


def real_encoder() -> tuple[E5Encoder, dict]:
    directory = model_directory()
    metadata = json.loads((directory / "model.meta.json").read_text(encoding="utf-8"))
    return E5Encoder(directory, metadata["revision"]), metadata


@pytest.mark.skipif(os.environ.get("RUN_E5_TESTS") != "1",
                    reason="нужны локальные веса e5-small и extra search")
def test_e5_embeddings_are_normalised_and_deterministic():
    encoder, _ = real_encoder()
    first = encoder.encode(["Дрель ручная электрическая", "Кресло офисное"], "passage: ")
    second = encoder.encode(["Дрель ручная электрическая", "Кресло офисное"], "passage: ")
    assert first.shape == (2, 384) and first.dtype == np.float32
    assert np.allclose(np.linalg.norm(first, axis=1), 1, atol=1e-5)
    assert np.array_equal(first, second)


@pytest.mark.skipif(os.environ.get("RUN_E5_TESTS") != "1",
                    reason="нужны локальные веса e5-small и extra search")
def test_e5_batching_does_not_change_the_vectors():
    encoder, _ = real_encoder()
    texts = ["дрель", "шуруповерт аккумуляторный", "куртка рабочая", "кресло", "рюкзак туристический"]
    together = encoder.encode(texts, "query: ")
    one_by_one = np.concatenate([encoder.encode([text], "query: ") for text in texts])
    assert np.allclose(together, one_by_one, atol=1e-5)


@pytest.mark.skipif(os.environ.get("RUN_E5_TESTS") != "1",
                    reason="нужны локальные веса e5-small и extra search")
def test_e5_prefixes_change_the_vector():
    """e5 обучена на префиксах: `query:` и `passage:` — разные векторы."""
    encoder, _ = real_encoder()
    text = "Дрель ручная электрическая"
    query = encoder.encode([text], "query: ")[0]
    passage = encoder.encode([text], "passage: ")[0]
    assert float(query @ passage) < 0.999


def test_e5_rejects_a_foreign_revision():
    """Веса и индекс должны быть одной ревизии: иначе энкодер не поднимается."""
    pytest.importorskip("torch")
    directory = model_directory()
    if not (directory / "model.meta.json").exists():
        pytest.skip("нет локальных весов")
    with pytest.raises(ValueError):
        E5Encoder(directory, "b" * 40)
