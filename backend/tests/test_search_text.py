"""Группа 4: факты пользователя в поисковом представлении.

Синтетические проверки сборки запроса (без каталога). Реальные проверки поиска —
`C:\\hack\\bench_search.py` (46 запросов) и `tests/verify_real_supply.py`.
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.components import search_text  # noqa: E402
from app.schemas import Fact  # noqa: E402


def fact(identifier: str, name: str, value: str, **kwargs) -> Fact:
    return Fact(id=identifier, name=name, value=value, unit=kwargs.pop("unit", None),
                raw=f"{name} {value}", source=kwargs.pop("source", "text"),
                subject="product", unknown=kwargs.pop("unknown", False), **kwargs)


def test_named_positive_facts_extend_the_query():
    """Уточнённая характеристика попадает в запрос вместе с исходным описанием."""
    text = search_text("дрель", [fact("f1", "Мощность", "650"), fact("f2", "Тип питания", "от сети")])
    assert text.startswith("дрель")
    assert "Мощность 650" in text and "Тип питания от сети" in text


def test_service_and_empty_values_are_not_in_the_query():
    """Технические ответы, «не знаю», отрицания и факты без имени в запрос не идут."""
    facts = [fact("a", "Тип", "accept:Кровельный", source="answer"),
             fact("b", "Материал", "reject", source="answer"),
             fact("c", "Мощность", "", unknown=True),
             fact("d", "", "650", unit="Вт"),
             fact("e", "Покрытие", "нет", negated=True),
             fact("f", "Мощность", "650")]
    text = search_text("дрель", facts)
    assert "accept:" not in text and "reject" not in text
    assert "не знаю" not in text and "Покрытие" not in text
    assert text.count("Мощность 650") == 1        # дубликаты не повторяются
    assert "650" in text


def test_facts_change_the_query_again_after_editing():
    """Правка факта меняет запрос: поиск не кэширует прежнее представление."""
    before = search_text("дрель", [fact("f1", "Мощность", "650")])
    after = search_text("дрель", [fact("f1", "Мощность", "1200")])
    assert "650" in before and "650" not in after and "1200" in after
    without = search_text("дрель", [])
    assert without.strip() == "дрель"


def test_description_is_always_kept():
    """Исходное описание остаётся в запросе, даже когда факты его дополняют."""
    text = search_text("молоток с деревянной рукояткой", [fact("f1", "Материал", "дерево")])
    assert "молоток с деревянной рукояткой" in text
