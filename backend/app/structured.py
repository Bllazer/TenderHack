"""Структурированные факты для алгоритма C (хендофф A -> C).

`Fact` остаётся форматом обмена для API и фронтенда и здесь не меняется. Этот
модуль проецирует факт в более богатую форму, которая нужна C: явный оператор
сравнения, типизированное значение, единица как объект, полярность, уверенность
извлечения и ключ определения.

Зачем это нужно, если C и так работает. Сейчас оператор живёт внутри строки:
«не менее 5» целиком лежит в `Fact.value`, и `checks.number()` заново выковыривает
его регэкспом. Работает, но сравнение не является полем и теряется при любой
переупаковке. Здесь оператор — отдельный атрибут.

`def_id` — идентичность характеристики. На этапе извлечения позиция ещё не выбрана,
поэтому ключ строится по нормализованному имени (`name:<canon>`). Если передать
позицию, ключ подменяется на позиционный код характеристики (`char_code`).
Неразрешённый ключ всегда начинается с `name:`, разрешённый — нет.

Уверенность — **не вероятность обученной модели**. Извлечение основано на правилах,
и score отражает силу сработавшего правила; правила перечислены в `confidence()`
и намеренно читаются глазами, чтобы их можно было откалибровать вручную.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, Field

from app.catalog import Position
from app.clean_supply import OKEI_SUPPORTED
from app.mapping import match_properties
from app.schemas import Fact
from app.search import canon
from app.units import get_unit

OPERATORS = {
    "не менее": ">=", "не ниже": ">=", ">=": ">=", "≥": ">=", ">": ">",
    "не более": "<=", "не выше": "<=", "<=": "<=", "≤": "<=", "<": "<",
}

BRACKET_INTERVAL = re.compile(r"^[\[(]\s*[+−-]?\d+(?:[.,]\d+)?\s*;\s*[+−-]?\d+(?:[.,]\d+)?\s*[\])]$")
RANGE_INTERVAL = re.compile(r"^[+−-]?\d+(?:[.,]\d+)?\s*(?:–|\.\.|…|-)\s*[+−-]?\d+(?:[.,]\d+)?$")
PLAIN_NUMBER = re.compile(r"^[+−-]?\d+(?:[.,]\d+)?$")
EXPLICIT_PAIR = re.compile(r"^\s*[^:=]{1,120}\s*[:=]")
NEGATION_MARKER = re.compile(r"(без|не\s+имеет|отсутств\w*)\s*$", re.IGNORECASE)

NAME_TO_OKEI = {canon(name): code for code, name in OKEI_SUPPORTED.items()}

Kind = Literal["number", "interval", "text", "unknown"]
Operator = Literal[">", ">=", "<", "<="]


class UnitRef(BaseModel):
    """Единица как объект: код ОКЕИ, каноническое имя, исходное написание и размерность."""

    okei_code: str | None = None
    name: str | None = None
    raw: str | None = None
    dimension: str | None = None
    factor: str | None = None


class StructuredFact(BaseModel):
    def_id: str
    name: str
    kind: Kind
    value: float | str | None = None
    unit: UnitRef | None = None
    operator: Operator | None = None
    polarity: Literal["affirmed", "negated"] = "affirmed"
    negation: str | None = None
    extraction_confidence: float = Field(ge=0.0, le=1.0)
    span: tuple[int, int] | None = None


def name_key(name: str) -> str:
    return "name:" + canon(name)


def split_operator(value: str) -> tuple[Operator | None, str]:
    """Отделить оператор сравнения от значения. Длинные маркеры проверяются первыми."""
    text = (value or "").strip()
    lowered = text.lower()
    for marker in sorted(OPERATORS, key=len, reverse=True):
        if lowered.startswith(marker):
            return OPERATORS[marker], text[len(marker):].strip()
    return None, text


def plain(value: str) -> str:
    return re.sub(r"\s+", "", value or "").replace("−", "-")


def classify(value: str) -> Kind:
    candidate = plain(value)
    if not candidate:
        return "unknown"
    if BRACKET_INTERVAL.match(candidate) or RANGE_INTERVAL.match(candidate):
        return "interval"
    if PLAIN_NUMBER.match(candidate):
        return "number"
    return "text"


def typed_value(value: str, kind: Kind) -> float | str | None:
    if kind == "number":
        try:
            return float(Decimal(plain(value).replace(",", ".")))
        except (InvalidOperation, ValueError):
            return None
    return value.strip() or None


def unit_ref(raw_unit: str | None) -> UnitRef | None:
    if not raw_unit:
        return None
    unit = get_unit(raw_unit)
    return UnitRef(okei_code=NAME_TO_OKEI.get(canon(unit.name)) if unit else None,
                   name=unit.name if unit else None, raw=raw_unit,
                   dimension=unit.dimension if unit else None,
                   factor=str(unit.factor) if unit else None)


def confidence(fact: Fact) -> float:
    """Сила сработавшего правила извлечения.

    1.00 — факт введён пользователем явно (input/answer);
    0.86 — явная пара «свойство: значение» в тексте;
    0.90 — имя свойства и единица подтверждены словарём;
    0.78 — имя свойства найдено, единица не подтверждена;
    0.50 — значение помечено как неизвестное;
    минус 0.06 — сработал отрицательный оборот («без …»).
    """
    if fact.source in ("input", "answer"):
        return 1.0
    if fact.unknown:
        return 0.50
    score = 0.90 if fact.unit else 0.78
    if EXPLICIT_PAIR.match(fact.raw or ""):
        score = 0.86
    if fact.negated:
        score = min(score, 0.80) - 0.06
    return round(score, 2)


def negation_marker(fact: Fact) -> str | None:
    if not fact.negated:
        return None
    match = NEGATION_MARKER.search(fact.raw or "")
    return match.group(1).strip().lower() if match else None


def structure(fact: Fact, position: Position | None = None) -> StructuredFact:
    """Спроецировать один факт. Если передана позиция, def_id разрешается в char_code."""
    def_id = name_key(fact.name)
    if position is not None:
        matches = match_properties(position, fact.name, fact.subject)
        if len(matches) == 1:
            def_id = matches[0].id
    operator, remainder = split_operator(fact.value or "")
    kind: Kind = "unknown" if fact.unknown else classify(remainder)
    return StructuredFact(
        def_id=def_id,
        name=fact.name,
        kind=kind,
        value=typed_value(remainder, kind),
        unit=unit_ref(fact.unit),
        operator=operator,
        polarity="negated" if fact.negated else "affirmed",
        negation=negation_marker(fact),
        extraction_confidence=confidence(fact),
        span=fact.span,
    )


def to_structured(facts: list[Fact], position: Position | None = None) -> list[StructuredFact]:
    """Спроецировать список фактов для алгоритма C.

    Без позиции def_id остаётся ключом по имени — это состояние до подтверждения
    категории. С позицией ключ становится позиционным кодом характеристики.
    """
    return [structure(fact, position) for fact in facts]
