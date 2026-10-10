"""Факты стадии A в формате контракта AlgorithmC / AlgorithmD.

`Fact` из `app/schemas.py` остаётся форматом обмена для API и фронтенда и здесь
не меняется. Этот модуль проецирует его в форму, которую требует контракт
алгоритмов C и D (`algorithms2/contract.py`).

Форма взята из контракта, а не придумана:

* `unit` — объект `{code, text}`, где `code` — код ОКЕИ, если он известен, а
  `text` — единица **как написана в исходном тексте**. Переводить не нужно,
  приведение к единице характеристики делает C.
* `kind` — одно из `number | text | boolean | range | number_set | text_set`.
  Интервал из текста — это `range` со значением `{"min": .., "max": ..}`.
* `extraction_confidence < 0.6` — факт проверяется, но не должен исключать код.

Про `fact_id`. Он обязателен, и вот почему. C отделяет «мягкие» факты так:

    soft_ids = {f.fact_id for f, _ in soft if f.fact_id}

Если у факта с низкой уверенностью `fact_id` пуст, он не попадает в `soft_ids`,
и ветка `if fact.fact_id in soft_ids` не срабатывает — факт уходит в `exclusions`,
то есть **жёстко исключает код**, хотя README обещает обратное. Поэтому `fact_id`
заполняется всегда, даже когда `def_id` ещё неизвестен.

`def_id` — код характеристики КТРУ, если сопоставление удалось. Пока позиция не
выбрана, он остаётся `None`: C умеет подхватить факт по имени
(`position.chars_by_name(fact.name)`), а выдуманный ключ вида `name:...` не является
кодом характеристики и только засоряет `def_ids()`.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.catalog import Position
from app.clean_supply import OKEI_SUPPORTED
from app.mapping import match_properties
from app.schemas import Fact
from app.search import canon
from app.units import get_unit

#: контракт contract.py: KINDS, OPERATORS, POLARITIES
KINDS = ("number", "text", "boolean", "range", "number_set", "text_set")
OPERATORS = (">=", "<=", ">", "<", "=", "!=")
POLARITIES = ("affirmed", "negated", "uncertain")

OPERATOR_MARKERS = {
    "не менее": ">=", "не ниже": ">=", ">=": ">=", "≥": ">=", ">": ">",
    "не более": "<=", "не выше": "<=", "<=": "<=", "≤": "<=", "<": "<",
    "равно": "=", "=": "=",
}

BRACKET_INTERVAL = re.compile(r"^[\[(]\s*([+−-]?\d+(?:[.,]\d+)?)\s*;\s*([+−-]?\d+(?:[.,]\d+)?)\s*[\])]$")
RANGE_INTERVAL = re.compile(r"^([+−-]?\d+(?:[.,]\d+)?)\s*(?:–|—|\.\.|…|-)\s*([+−-]?\d+(?:[.,]\d+)?)$")
PLAIN_NUMBER = re.compile(r"^[+−-]?\d+(?:[.,]\d+)?$")
EXPLICIT_PAIR = re.compile(r"^\s*[^:=]{1,120}\s*[:=]")
NEGATION_MARKER = re.compile(r"(без|не\s+имеет|отсутств\w*)\s*$", re.IGNORECASE)

NAME_TO_OKEI = {canon(name): code for code, name in OKEI_SUPPORTED.items()}


class UnitRef(BaseModel):
    """Единица по контракту: код ОКЕИ и написание из текста."""

    code: str | None = None
    text: str | None = None


class StructuredFact(BaseModel):
    """Факт стадии A. Имена полей совпадают с `algorithms2.contract.Fact`."""

    kind: Literal["number", "text", "boolean", "range", "number_set", "text_set"]
    value: Any = None
    name: str | None = None
    def_id: str | None = None
    unit: UnitRef | None = None
    operator: Literal[">=", "<=", ">", "<", "=", "!="] | None = None
    polarity: Literal["affirmed", "negated", "uncertain"] = "affirmed"
    negation: str | None = None
    span: list[int] | None = None
    extraction_method: str | None = None
    extraction_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    definition_alternatives: list = Field(default_factory=list)
    fact_id: str | None = None


def plain(value: str) -> str:
    return re.sub(r"\s+", "", value or "").replace("−", "-")


def to_number(value: str) -> float | None:
    try:
        import math
        number = Decimal(plain(value).replace(",", "."))
        result = float(number)
        return result if number.is_finite() and math.isfinite(result) else None
    except (InvalidOperation, ValueError):
        return None


def split_operator(value: str) -> tuple[str | None, str]:
    """Отделить оператор сравнения от значения. Длинные маркеры проверяются первыми."""
    text = (value or "").strip()
    lowered = text.lower()
    for marker in sorted(OPERATOR_MARKERS, key=len, reverse=True):
        if lowered.startswith(marker):
            return OPERATOR_MARKERS[marker], text[len(marker):].strip()
    return None, text


def parse_range(value: str) -> dict | None:
    candidate = plain(value)
    match = BRACKET_INTERVAL.match(candidate) or RANGE_INTERVAL.match(candidate)
    if not match:
        return None
    low, high = to_number(match.group(1)), to_number(match.group(2))
    if low is None or high is None:
        return None
    return {"min": low, "max": high,
            "min_inclusive": not candidate.startswith("("), "max_inclusive": not candidate.endswith(")")}


def unit_ref(raw_unit: str | None) -> UnitRef | None:
    """Единица как в тексте плюс код ОКЕИ, если написание распознано справочником."""
    if not raw_unit:
        return None
    unit = get_unit(raw_unit)
    return UnitRef(code=NAME_TO_OKEI.get(canon(unit.name)) if unit else None, text=raw_unit)


def extract_method(fact: Fact) -> str:
    if fact.source in ("input", "answer"):
        return "user_input"
    if EXPLICIT_PAIR.match(fact.raw or ""):
        return "explicit_pair"
    return "dictionary_match"


def confidence(fact: Fact) -> float:
    """Сила сработавшего правила извлечения, не вероятность обученной модели.

    1.00 — факт введён пользователем явно;
    0.86 — явная пара «свойство: значение» в тексте;
    0.90 — имя свойства и единица подтверждены словарём;
    0.78 — имя свойства найдено, единица не подтверждена;
    0.50 — значение помечено как неизвестное (**ниже порога 0.6**, поэтому не исключает);
    минус 0.06 — сработал отрицательный оборот («без …»).
    """
    if fact.unknown:
        return 0.50
    if fact.source in ("input", "answer"):
        return 1.0
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
    """Спроецировать один факт. С позицией `def_id` становится кодом характеристики."""
    def_id = None
    if position is not None:
        matches = match_properties(position, fact.name, fact.subject)
        if len(matches) == 1:
            def_id = matches[0].id

    operator, remainder = split_operator(fact.value or "")

    kind = "text"
    value: Any = (fact.value or "").strip()
    bounds = parse_range(remainder)
    invalid_bounds = bool(bounds and (bounds["min"] > bounds["max"] or
        bounds["min"] == bounds["max"] and not (bounds["min_inclusive"] and bounds["max_inclusive"])))
    number = to_number(remainder)
    if bounds is not None and not invalid_bounds:
        kind, value = "range", bounds
    elif number is not None:
        kind, value = "number", number

    return StructuredFact(
        kind=kind,
        value=value,
        name=fact.name or None,
        def_id=def_id,
        unit=unit_ref(fact.unit),
        operator=operator if kind == "number" else None,
        polarity="uncertain" if fact.unknown or invalid_bounds else "negated" if fact.negated else "affirmed",
        negation=negation_marker(fact),
        span=list(fact.span) if fact.span else None,
        extraction_method=extract_method(fact),
        extraction_confidence=confidence(fact),
        definition_alternatives=[],
        # Заполняется всегда: без него мягкий факт жёстко исключает код в AlgorithmC.
        fact_id=fact.id,
    )


def to_structured(facts: list[Fact], position: Position | None = None) -> list[StructuredFact]:
    """Факты для алгоритмов C и D.

    Без позиции `def_id` пуст, и C подхватит факт по имени. С позицией он
    становится кодом характеристики, и сопоставление идёт по нему.
    """
    return [structure(fact, position) for fact in facts]


#: версия схемы документа стадии A, как в samples/stage_model_output.json
SCHEMA_VERSION = "1.0"


def stage_item(position_code: str, title: str, pool: list[str],
               facts: list[StructuredFact]) -> dict:
    """Один товар в документе стадии A: позиция, её пул и факты по ней."""
    return {
        "position_code": position_code,
        "title": title,
        "pool": list(pool),
        "facts": [fact.model_dump() for fact in facts],
    }


def stage_output(items: list[dict], source: str) -> dict:
    """Документ стадии A целиком — та форма, которую читает validation_run.

    Именно этот JSON является выходом модели: обёртка с версией схемы и список
    товаров, у каждого свой пул и свои факты.
    """
    return {"source": source, "schema_version": SCHEMA_VERSION, "items": list(items)}
