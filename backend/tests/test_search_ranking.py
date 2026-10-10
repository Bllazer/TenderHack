"""Регрессии стадии B: порядок RRF, малые каталоги, варианты запроса, назначения.

Каждый тест привязан к конкретному дефекту из независимого ревью e382611.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.search import CharNgramIndex, _rank_order, canon, query_variants
from app.synonyms import purpose_phrases, search_part


def test_rrf_score_ranks_above_quality():
    """Кандидат с большим RRF-score не может получить худший ранг.

    Числа взяты из независимого ревью: score 0.038733454862487124 стоял четвёртым,
    а 0.038099747474747474 — первым, потому что сортировка шла по `quality`.
    """
    categories = [SimpleNamespace(id=f"id{index}") for index in range(4)]
    ranks = {
        0: 0.038099747474747474,
        1: 0.0163,
        2: 0.0163,
        3: 0.038733454862487124,
    }
    order = {
        0: [("BM25", 1)],
        1: [("BM25", 2), ("CHAR", 2)],
        2: [("E5", 3), ("CHAR", 3)],
        3: [("E5", 1)],
    }
    assert _rank_order(ranks, order, categories)[0] == 3


def test_rrf_score_is_the_primary_key():
    """Даже при равенстве прочих признаков решает score, а не число каналов."""
    categories = [SimpleNamespace(id=f"id{index}") for index in range(3)]
    ranks = {0: 0.5, 1: 0.2, 2: 0.1}
    order = {0: [("BM25", 5)], 1: [("BM25", 1), ("E5", 1)], 2: [("BM25", 2)]}
    assert _rank_order(ranks, order, categories) == [0, 1, 2]


def test_quality_resolves_only_ties():
    """При равном score первым идёт тот, у кого больше согласия каналов."""
    categories = [SimpleNamespace(id=f"id{index}") for index in range(3)]
    score = 1 / 61
    ranks = {0: score, 1: score, 2: score}
    order = {0: [("BM25", 1)], 1: [("BM25", 1), ("CHAR", 2)], 2: [("E5", 3), ("CHAR", 3)]}
    assert _rank_order(ranks, order, categories) == [1, 2, 0]


def test_find_returns_non_increasing_scores(engine):
    """Инвариант: выдача `find` не может идти против своей же оценки."""
    _, search = engine
    for query in ("письменный стол", "рабочая куртка", "стол для офиса"):
        scores = [candidate.rank_score for candidate in search.find(query)]
        assert scores == sorted(scores, reverse=True), query


def test_small_catalog_char_channel_still_scores():
    """Маленький каталог: с min_df=2 все n-граммы уникального названия удалялись,
    и точное название получало нулевую оценку. С min_df=1 канал работает."""
    names = ["Шуруповерт", "Кресло офисное", "Рюкзак туристический", "Дрель ручная"]
    index = CharNgramIndex(names)
    for position, name in enumerate(names):
        scores = index.scores(name)
        assert scores[position] > 0, name
        assert scores.argmax() == position, name


def test_small_catalog_with_min_df_two_would_be_blind():
    """Страховка от отката: при min_df=2 этот каталог действительно слеп."""
    names = ["Шуруповерт", "Кресло офисное", "Рюкзак туристический", "Дрель ручная"]
    blind = CharNgramIndex(names, min_df=2)
    assert not blind.scores("Шуруповерт").any()


def test_large_catalog_keeps_every_gram():
    """Полный индекс не теряет граммы: у документа с общими n-граммами всё на месте."""
    index = CharNgramIndex(["Кресло офисное", "Кресло офисное кожаное", "Офисное кресло"])
    assert index.scores("Кресло офисное").argmax() == 0
    assert all(index.scores(name).any() for name in
               ("Кресло офисное", "Кресло офисное кожаное", "Офисное кресло"))


def test_layout_variant_is_generated_and_original_kept():
    assert query_variants("lhtkm") == ("lhtkm", "дрель")
    variants = query_variants("iehegjdthn")
    assert variants[0] == "iehegjdthn" and "шуруповерт" in variants


def test_layout_variant_is_not_applied_to_cyrillic():
    """Смешанный или русский текст — не ошибка раскладки."""
    assert query_variants("дрель электрическая") == ("дрель электрическая",)
    assert query_variants("ГОСТ 12.2.013") == ("ГОСТ 12.2.013",)


def test_homoglyph_variant_only_inside_cyrillic_tokens():
    variants = query_variants("дрeль электрическая")
    assert variants[0] == "дрeль электрическая"
    assert "дрель электрическая" in variants
    assert query_variants("Makita")[0] == "Makita"
    assert all(canon(variant) != "" for variant in query_variants("Makita"))


def test_codes_and_identifiers_are_protected():
    """Код, ГОСТ, артикул не переписываются поисковым вариантом."""
    for query in ("GOST 12.2.013", "28.24.11.000-00000003", "HR2450FT"):
        for variant in query_variants(query):
            for token in query.split():
                if any(char.isdigit() for char in token):
                    assert token in variant


def test_purpose_layer_generalises_and_does_not_pollute():
    """Слой назначений работает по классам и не задевает чужие классы."""
    backpack = " ".join(purpose_phrases("Рюкзак туристический"))
    assert "похода" in backpack and "спине" in backpack
    screwdriver = " ".join(purpose_phrases("Шуруповерт"))
    assert "завинчивания" in screwdriver and "саморезов" in screwdriver
    assert search_part("Кресло офисное") is not None
    # Чужие классы не должны получать чужие назначения.
    assert search_part("Кресло-коляска механическая") is None
    assert search_part("Перфоратор десны, многоразового использования") is None
    assert search_part("Винтовой конвейер") is None
    # Хирургическая дрель — отдельная ветка медицинских изделий, а не строительный
    # инструмент: слой её не задевает (иначе она поднималась в выдаче по запросам
    # про обычную дрель).
    assert search_part("Дрель хирургическая ручная") is None
    assert search_part("Дрель/пила хирургическая универсальная") is None
    assert search_part("Дрель ручная электрическая") is not None


def test_purpose_phrases_are_deterministic():
    assert purpose_phrases("Шкаф деревянный для документов") == \
        purpose_phrases("Шкаф деревянный для документов")
    assert len(set(purpose_phrases("Шуруповерт"))) == len(purpose_phrases("Шуруповерт"))
