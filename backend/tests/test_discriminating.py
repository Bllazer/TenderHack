"""Тесты различимости характеристики и честной формулировки вопроса (без каталога)."""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.catalog import AllowedValue, Position, Property  # noqa: E402
from app.questions import discriminates  # noqa: E402


def size_property(values: tuple[str, ...], identifier: str = "size") -> Property:
    return Property(id=identifier, name="Рост", value_type="number", required=True, verified=True,
                    evidence="test", kind="2", allowed=tuple(
                        AllowedValue(kind="number", value=value, unit="см", raw_unit="см", source="test")
                        for value in values))


def position(code: str, values: tuple[str, ...]) -> Position:
    return Position(code=code, name=f"Позиция {code}", source_path="test", status="actual",
                    is_template=False, properties=(size_property(values),))


def test_same_values_in_all_positions_do_not_discriminate():
    """Несколько размеров ведут к одному коду: выбор размера код не меняет."""
    positions = (position("A", ("110", "116", "122")),)
    key = ("size", "product")
    assert not discriminates(key, positions)


def test_values_that_exclude_a_position_do_discriminate():
    """Если значение отсекает позицию, характеристика действительно различает."""
    positions = (position("A", ("110", "116")), position("B", ("122", "128")))
    key = ("size", "product")
    assert discriminates(key, positions)


def test_two_positions_with_identical_values_do_not_discriminate():
    """Две позиции с одинаковым набором значений: вопрос всё равно ничего не решает."""
    positions = (position("A", ("110", "116")), position("B", ("110", "116")))
    key = ("size", "product")
    assert not discriminates(key, positions)
