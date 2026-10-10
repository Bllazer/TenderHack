"""Факты стадии A в формате контракта AlgorithmC / AlgorithmD.

`Fact` из `app/schemas.py` остаётся форматом обмена для API и фронтенда и здесь
не меняется. Этот модуль проецирует его в форму, которую требует контракт
алгоритмов C и D (`algorithms/contract.py`).

Форма взята из контракта, а не придумана:

* `unit` — объект `{code, text}`, где `code` — код ОКЕИ, если он известен, а
  `text` — единица **как написана в исходном тексте**. Переводить не нужно,
  приведение к единице характеристики делает C.
* `kind` — одно из `number | text | boolean | range | number_set | text_set`.
  Интервал из текста — это `range` со значением `{"min": .., "max": ..}`.
* `extraction_confidence < 0.6` — факт проверяется, но не должен исключать код.

Про `raw`. Полезная нагрузка A дополнена полем `raw` — исходным текстом факта.
Дополнение аддитивное и обратно совместимое: `Fact.from_dict` в контракте C
выбирает известные ему ключи и лишние игнорирует, старые потребители не ломаются.

Про строгие границы интервалов. Открытость границы выражается ключами
`min_inclusive`/`max_inclusive` — это **согласованная с владельцем C нотация**:
`AlgorithmC.check_fact` читает именно их (`low_closed`/`high_closed`), а
`contract.Fact.problems()` отвергает перевёрнутый или пустой интервал. Поэтому
строгий, но непустой интервал (`(1;2)`) уходит как обычный `range`, остаётся
жёстким и проверяется C по-настоящему, а не теряет смысл.

Перевёрнутый (`[8;2]`) или пустой (`(750;750)`) интервал — ошибка ввода. В
контракт такой `range` отдать нельзя (его отвергает `problems()`), поэтому факт
уходит текстом с сохранением исходного написания, `polarity="uncertain"` и
понятной причиной в `extraction_method` (`reversed_interval` / `empty_interval`):
он не исключает код и уходит на уточнение, а введённое значение не подменяется.

Про `min_notation`/`max_notation`. Эти ключи остаются во внутреннем представлении
индекса КТРУ (`algorithms/ktru_index.py`, где они описывают допустимые значения
характеристики), но в фактах стадии A не используются: у контракта C для факта
принята пара `min_inclusive`/`max_inclusive`. Не смешивать.

Про `fact_id`. Он обязателен, и вот почему. C отделяет «мягкие» факты так:

    soft_ids = {f.fact_id for f, _ in soft if f.fact_id}

Если у факта с низкой уверенностью `fact_id` пуст, он не попадает в `soft_ids`,
и ветка `if fact.fact_id in soft_ids` не срабатывает — факт уходит в
`exclusions`, то есть **жёстко исключает код**, хотя README обещает обратное.
Поэтому `fact_id` заполняется всегда, даже когда `def_id` ещё неизвестен.

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

from app.catalog import Position, Property
from app.clean_supply import OKEI_SUPPORTED
from app.extraction import AMBIGUOUS_SEPARATOR, lemmas
from app.mapping import match_properties
from app.schemas import Fact
from app.search import canon
from app.units import get_unit

#: контракт contract.py: KINDS, OPERATORS, POLARITIES
KINDS = ("number", "text", "boolean", "range", "number_set", "text_set")
OPERATORS = (">=", "<=", ">", "<", "=", "!=")
POLARITIES = ("affirmed", "negated", "uncertain")

#: уверенность неопределённого факта: ниже порога жёсткости 0.6 из контракта C
UNCONFIRMED_CONFIDENCE = 0.50

#: значения «да/нет» через canon: регистр и ё/е приводятся к одному виду
BOOLEAN_VALUES = {"да": True, "нет": False}

#: Значения, которыми пользователь выражает «не знаю». Приходят и как текст от
#: app.extraction (там выставляется Fact.unknown), и как ответ пользователя:
#: app/analysis.py ставит unknown только при ПУСТОМ значении, поэтому «не знаю»,
#: набранное текстом, приходит с unknown=False. Такое значение не должно считаться
#: подтверждённым ни при каком источнике (ревью A4/A5) — иначе ответ «не знаю»
#: становится жёстким фактом и исключает коды.
UNKNOWN_VALUES = frozenset({"", "не знаю", "не задано", "(не задано)", "неизвестно",
                            "нет данных", "не установлено"})

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
    """Факт стадии A. Имена полей совпадают с `algorithms.contract.Fact`.

    `raw` — аддитивное поле: контракт C его не знает и игнорирует при
    `Fact.from_dict`, зато исходный текст факта больше нигде не теряется.
    """

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
    #: исходный текст факта как в описании; у контракта C такого поля нет
    raw: str | None = None


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


def parse_range(value: str) -> tuple[dict, str | None] | None:
    """Границы интервала и причина, по которой интервал не может быть жёстким.

    Границы НЕ переставляются: «[8;2]» остаётся перевёрнутым интервалом, а не
    «[2;8]», и отдаётся как есть. Открытость границ сохраняется ключами
    `min_inclusive`/`max_inclusive` — это согласованная с C нотация
    (`AlgorithmC.check_fact` читает именно их), поэтому строгий интервал выразим
    точно и факт может остаться жёстким. Пустой интервал — это `(750;750)`, а не
    точка 750.

    Возвращает `(bounds, reason)`; `reason is None` — интервал выразим в
    контракте C точно. Причина не None означает ошибку ввода (перевёрнутый или
    пустой интервал): такой range контракт отвергает, поэтому факт отдаётся
    текстом с сохранением исходного написания и уходит на уточнение.
    """
    candidate = plain(value)
    bracket = BRACKET_INTERVAL.match(candidate)
    match = bracket or RANGE_INTERVAL.match(candidate)
    if not match:
        return None
    low, high = to_number(match.group(1)), to_number(match.group(2))
    if low is None or high is None:
        return None
    left_strict = bool(bracket) and candidate.startswith("(")
    right_strict = bool(bracket) and candidate.endswith(")")
    bounds: dict[str, Any] = {"min": low, "max": high,
                              "min_inclusive": not left_strict, "max_inclusive": not right_strict}
    if low > high:
        return bounds, "reversed_interval"
    if low == high and (left_strict or right_strict):
        return bounds, "empty_interval"
    return bounds, None


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

    1.00 — факт введён пользователем явно (но только если он не помечен неизвестным);
    0.86 — явная пара «свойство: значение» в тексте;
    0.90 — имя свойства и единица подтверждены словарём;
    0.78 — имя свойства найдено, единица не подтверждена;
    0.50 — значение помечено как неизвестное (**ниже порога 0.6**, поэтому не исключает);
    минус 0.06 — сработал отрицательный оборот («без …»).

    Проверка `unknown` идёт первой: источник «input»/«answer» сам по себе не делает
    неизвестное значение известным. Значение-заглушка («не знаю») распознаётся и без
    флага `unknown`: так приходит ответ пользователя, набранный текстом.
    """
    if fact.unknown or (fact.values is None and canon(fact.value or "") in UNKNOWN_VALUES):
        return UNCONFIRMED_CONFIDENCE
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


def matched_property(fact: Fact, position: Position | None) -> Property | None:
    """Единственная сопоставленная характеристика позиции, иначе None.

    Сначала точное равенство имени/алиаса (`app.mapping`) — без «починки» похожих
    написаний: «Мошность» остаётся «Мошностью» и характеристику не получает.

    Если точной формы нет, сравниваются последовательности лемм тем же анализатором,
    которым работает разбор (`app.extraction.lemmas`): «мощностью 650 Вт» — это
    проверенная словарём форма слова «Мощность», и факт должен получить её код.
    Привязка берётся, только если такая последовательность лемм ровно одна.
    Живой путь API (`app.mapping.map_facts` → `app/checks.py`) здесь не меняется.
    """
    if position is None:
        return None
    matches = match_properties(position, fact.name, fact.subject)
    if matches:
        return matches[0] if len(matches) == 1 else None
    target = lemmas(fact.name or "")
    if not target:
        return None
    candidates = [prop for prop in position.properties if prop.subject == fact.subject
                  and (lemmas(prop.name) == target
                       or any(lemmas(alias) == target for alias in prop.aliases))]
    return candidates[0] if len(candidates) == 1 else None


def is_boolean_property(prop: Property | None) -> bool:
    """Булева ли характеристика: `value_type == "boolean"` или допустимые ровно {да, нет}."""
    if prop is None:
        return False
    if prop.value_type == "boolean":
        return True
    values = {canon(item.value) for item in prop.allowed if item.kind == "enum" and item.value}
    return values == {"да", "нет"}


def structure(fact: Fact, position: Position | None = None) -> StructuredFact:
    """Спроецировать один факт. С позицией `def_id` становится кодом характеристики."""
    prop = matched_property(fact, position)
    def_id = prop.id if prop is not None else None

    operator, remainder = split_operator(fact.value or "")
    raw_value = (fact.value or "").strip()

    kind: str = "text"
    value: Any = raw_value
    #: причина, по которой факт не может быть жёстким; она же становится extraction_method
    reason: str | None = None

    if fact.values is not None:
        numbers = [to_number(value) for value in fact.values]
        kind = "number_set" if prop and prop.value_type == "number" and all(v is not None for v in numbers) else "text_set"
        value = numbers if kind == "number_set" else fact.values

    if fact.unknown or (fact.values is None and canon(raw_value) in UNKNOWN_VALUES):
        # A4/A5/A9: неизвестное значение сохраняется как в тексте и не подтверждается
        # ни источником «input»/«answer», ни похожестью на число. Значение-заглушку
        # («не знаю») распознаём и без флага unknown — так приходит ответ текстом.
        reason = "ambiguous_separator" if AMBIGUOUS_SEPARATOR.search(raw_value) else "unknown_value"
    elif fact.values is None:
        parsed = parse_range(remainder)
        number = to_number(remainder)
        if parsed is not None:
            bounds, bounds_reason = parsed
            if bounds_reason is None:
                # Строгость выразима: min_inclusive/max_inclusive читает C.
                kind, value = "range", bounds
            else:
                # Перевёрнутый или пустой интервал — ошибка ввода. Такой range
                # контракт отвергает (`problems()`), поэтому в контракт он не
                # уходит: сохраняем исходный текст и уводим факт на уточнение.
                reason = bounds_reason
        elif number is not None:
            kind, value = "number", number

    if reason is None and fact.unit and get_unit(fact.unit) is None:
        # A11: единица не распознана справочником — факт сохраняется, но не подтверждается.
        reason = "unsupported_unit"

    if reason is None:
        if kind == "text" and prop is not None and is_boolean_property(prop) and canon(value) in BOOLEAN_VALUES:
            # A6: «да/нет» — булево только там, где характеристика булева.
            kind, value = "boolean", BOOLEAN_VALUES[canon(value)]
        elif kind == "text" and prop is not None and prop.kind == "3":
            # A10: множественные значения (kind «3» — multiple choice) сохраняются списком.
            from app.selected_values import selected_values
            values = selected_values(prop, fact)
            if len(values) > 1:
                kind, value = "text_set", values

    if reason is not None:
        polarity = "uncertain"
        score = min(confidence(fact), UNCONFIRMED_CONFIDENCE)
        method = reason
    else:
        polarity = "negated" if fact.negated else "affirmed"
        score = confidence(fact)
        method = extract_method(fact)

    return StructuredFact(
        kind=kind,
        value=value,
        name=fact.name or None,
        def_id=def_id,
        unit=unit_ref(fact.unit),
        operator=operator if kind == "number" else None,
        polarity=polarity,
        negation=negation_marker(fact),
        span=list(fact.span) if fact.span else None,
        extraction_method=method,
        extraction_confidence=score,
        definition_alternatives=[],
        # Заполняется всегда: без него мягкий факт жёстко исключает код в AlgorithmC.
        fact_id=fact.id,
        # Аддитивное поле: контракт C игнорирует неизвестные ключи.
        raw=fact.raw or None,
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
