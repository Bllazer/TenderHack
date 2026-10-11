"""«Не знаю» и похожие имена характеристик: подсказка исправления не должна зацикливаться.

Реальный сценарий: у молотка есть «Масса молотка» и «Масса головки молотка». Пользователь
отвечает «Не знаю» про массу молотка, а система предлагала исправить имя на «Масса головки
молотка» — и повторяла этот вопрос бесконечно.
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.catalog import AllowedValue, Position, Property  # noqa: E402
from app.correction_flow import resolve_suggestions  # noqa: E402
from app.schemas import Fact  # noqa: E402


def interval_property(identifier: str, name: str, lower: str, upper: str, unit: str = "кг") -> Property:
    return Property(id=identifier, name=name, value_type="number", required=False, verified=True,
                    evidence="test", kind="1",
                    allowed=(AllowedValue(kind="interval", lower=lower, upper=upper, unit=unit,
                                          raw_unit=unit, source="test"),))


def hammer_position() -> Position:
    return Position(code="25.73.30.141-00000011", name="Молоток", source_path="test", status="actual",
                    is_template=False,
                    properties=(interval_property("m1", "Масса молотка", "0.1", "5"),
                                interval_property("m2", "Масса головки молотка", "0.1", "3")))


def test_unknown_answer_does_not_propose_a_name_correction():
    """«Не знаю» по «Масса молотка» не превращается в исправление на «Масса головки молотка»."""
    fact = Fact(id="answer:q", name="Масса молотка", value="", unit="кг", raw="не знаю",
                source="answer", subject="product", unknown=True)
    pending, rejected, accepted = resolve_suggestions("cat", (hammer_position(),), [fact], [])
    assert pending == [] and accepted == []
    assert fact.name == "Масса молотка"


def test_exact_property_name_is_never_corrected():
    """Точное имя характеристики позиции не «исправляем» на соседнее похожее."""
    fact = Fact(id="text:1", name="Масса молотка", value="2", unit="кг", raw="масса молотка 2 кг",
                source="text", subject="product", unknown=False)
    pending, rejected, accepted = resolve_suggestions("cat", (hammer_position(),), [fact], [])
    assert pending == [] and accepted == []


def test_similar_but_wrong_name_still_gets_a_correction():
    """Похожее, но не совпавшее имя по-прежнему исправляется: механизм не выключен."""
    fact = Fact(id="text:2", name="Маса молотка", value="2", unit="кг", raw="маса молотка 2 кг",
                source="text", subject="product", unknown=False)
    pending, rejected, accepted = resolve_suggestions("cat", (hammer_position(),), [fact], [])
    assert pending and pending[0][1] is not None
    assert "Масса молотка" in pending[0][1].title or "Масса головки молотка" in pending[0][1].title
