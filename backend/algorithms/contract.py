"""Единый формат данных для AlgorithmC и AlgorithmD.

Здесь только формы данных и их проверка — никакой логики сужения и вопросов.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

SCHEMA_VERSION = "1.0"

#: ниже этой уверенности факт не исключает код, а идёт в уточнение
HARD_FACT_MIN_CONFIDENCE = 0.6


# --------------------------------------------------------------------- единица
@dataclass
class Unit:
    code: Optional[str] = None          # код ОКЕИ, если известен
    text: Optional[str] = None          # как написано в исходном тексте

    def to_dict(self) -> dict:
        return {"code": self.code, "text": self.text}


# ------------------------------------------------------------------ вход от A
@dataclass
class Fact:
    """Свойство товара, извлечённое моделью (стадия A) и сопоставленное со справочником.

    Обязательные поля: kind, value. Остальное уточняет стадию сопоставления.
    """

    kind: str                            # number | text | boolean | range | number_set | text_set
    value: Any
    name: Optional[str] = None           # имя свойства из текста
    def_id: Optional[str] = None         # код характеристики КТРУ, если сопоставление удалось
    unit: Optional[Unit] = None
    operator: Optional[str] = None       # >=, <=, >, <, =, !=  («не менее 2»)
    polarity: str = "affirmed"           # affirmed | negated | uncertain
    negation: Optional[str] = None
    span: Optional[list] = None
    extraction_method: Optional[str] = None
    extraction_confidence: float = 1.0
    definition_alternatives: list = field(default_factory=list)
    fact_id: Optional[str] = None

    KINDS = ("number", "text", "boolean", "range", "number_set", "text_set")
    OPERATORS = (">=", "<=", ">", "<", "=", "!=")
    POLARITIES = ("affirmed", "negated", "uncertain")

    # ------------------------------------------------------------------ проверки
    def problems(self) -> list:
        out = []
        if self.kind not in self.KINDS:
            out.append("kind=%r вне %s" % (self.kind, list(self.KINDS)))
        if self.operator is not None and self.operator not in self.OPERATORS:
            out.append("operator=%r вне %s" % (self.operator, list(self.OPERATORS)))
        if self.polarity not in self.POLARITIES:
            out.append("polarity=%r вне %s" % (self.polarity, list(self.POLARITIES)))
        if not isinstance(self.extraction_confidence, (int, float)) or not 0.0 <= self.extraction_confidence <= 1.0:
            out.append("extraction_confidence=%r вне 0..1" % (self.extraction_confidence,))
        if self.kind in ("number", "number_set"):
            vals = self.value if self.kind == "number_set" else [self.value]
            if not isinstance(vals, list):
                out.append("для %s нужен список чисел" % self.kind)
            else:
                for v in vals:
                    if isinstance(v, bool) or not isinstance(v, (int, float)):
                        out.append("значение %r не число, а %s" % (v, type(v).__name__))
        elif self.kind == "range":
            if not (isinstance(self.value, dict) and ("min" in self.value or "max" in self.value)):
                out.append("для range нужен {'min':..,'max':..}")
        elif self.kind == "text" and not isinstance(self.value, str):
            out.append("для text нужна строка")
        elif self.kind == "text_set" and not (isinstance(self.value, list) and all(isinstance(x, str) for x in self.value)):
            out.append("для text_set нужен список строк")
        elif self.kind == "boolean" and not isinstance(self.value, bool):
            out.append("для boolean нужно true/false")
        return out

    @property
    def is_hard(self) -> bool:
        """Может ли факт жёстко исключать код."""
        return self.polarity == "affirmed" and self.extraction_confidence >= HARD_FACT_MIN_CONFIDENCE and not self.problems()

    def def_ids(self) -> list:
        ids = [self.def_id] if self.def_id else []
        for alt in self.definition_alternatives or []:
            if isinstance(alt, dict) and alt.get("def_id"):
                ids.append(alt["def_id"])
        return ids

    @classmethod
    def from_dict(cls, raw: dict) -> "Fact":
        unit = raw.get("unit")
        if isinstance(unit, str):
            unit = {"code": None, "text": unit}
        return cls(
            kind=raw.get("kind"),
            value=raw.get("value"),
            name=raw.get("name"),
            def_id=raw.get("def_id"),
            unit=Unit(**unit) if isinstance(unit, dict) else None,
            operator=raw.get("operator"),
            polarity=raw.get("polarity") or "affirmed",
            negation=raw.get("negation"),
            span=raw.get("span"),
            extraction_method=raw.get("extraction_method"),
            extraction_confidence=raw.get("extraction_confidence", 1.0),
            definition_alternatives=raw.get("definition_alternatives") or [],
            fact_id=raw.get("fact_id"),
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["unit"] = self.unit.to_dict() if self.unit else None
        return d


# ------------------------------------------------------- вход от B / стадии связей
@dataclass
class Pool:
    """Пул конечных позиций КТРУ, раскрытый по связям до проверки ограничений."""

    codes: list
    family: Optional[str] = None
    catalog_version: str = "ktru_clean_v3.1"
    scenario_date: str = "2026-10-09"
    scope_complete: bool = True          # весь ли пул раскрыт, без обрезки лимитом UI

    @classmethod
    def from_dict(cls, raw: dict) -> "Pool":
        return cls(
            codes=list(raw.get("codes") or raw.get("pool") or []),
            family=raw.get("family"),
            catalog_version=raw.get("catalog_version", "ktru_clean_v3.1"),
            scenario_date=raw.get("scenario_date", "2026-10-09"),
            scope_complete=bool(raw.get("scope_complete", True)),
        )


# ------------------------------------------------------------------ результат C
@dataclass
class Exclusion:
    fact_id: Optional[str]
    property: Optional[str]
    actual: str
    rule: str
    def_id: Optional[str] = None
    position: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "fact_id": self.fact_id,
            "property": self.property,
            "actual": self.actual,
            "rule": self.rule,
            "source": {"def_id": self.def_id, "position": self.position},
        }


@dataclass
class Unresolved:
    fact_id: Optional[str]
    property: Optional[str]
    reason: str

    def to_dict(self) -> dict:
        return {"fact_id": self.fact_id, "property": self.property, "reason": self.reason}


@dataclass
class Candidate:
    code: str
    verdict: str                           # compatible | incompatible | unknown
    exclusions: list = field(default_factory=list)
    unresolved: list = field(default_factory=list)
    missing_required: list = field(default_factory=list)
    resolve_blocked: bool = False
    resolve_blocked_reason: Optional[list] = None
    future: bool = False

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "verdict": self.verdict,
            "exclusions": [e.to_dict() for e in self.exclusions],
            "unresolved": [u.to_dict() for u in self.unresolved],
            "missing_required": self.missing_required,
            "resolve_blocked": self.resolve_blocked,
            "resolve_blocked_reason": self.resolve_blocked_reason,
            "future": self.future,
        }


@dataclass
class FlagSet:
    type_verified: bool = False
    ktru_unique: bool = False
    fields_complete: bool = False
    value_verified: bool = False
    catalog_scope_complete: bool = True
    value_indistinguishable: bool = False
    blocking: list = field(default_factory=list)

    def can_resolve(self) -> bool:
        return (self.type_verified and self.ktru_unique and self.fields_complete
                and self.value_verified and self.catalog_scope_complete and not self.blocking)

    def to_dict(self) -> dict:
        return {
            "type_verified": self.type_verified,
            "ktru_unique": self.ktru_unique,
            "fields_complete": self.fields_complete,
            "value_verified": self.value_verified,
            "catalog_scope_complete": self.catalog_scope_complete,
            "value_indistinguishable": self.value_indistinguishable,
            "blocking_issues": self.blocking,
        }


@dataclass
class CResult:
    catalog_version: str
    pool: list                             # коды, поданные на вход
    counts: dict
    flags: FlagSet
    selected_ktru_code: Optional[str]
    candidates: list                       # list[Candidate]
    trace: list                            # что отсеял каждый факт

    @property
    def survivors(self) -> list:
        return [c.code for c in self.candidates if c.verdict != "incompatible"]

    @property
    def survivor_objects(self) -> list:
        return [c for c in self.candidates if c.verdict != "incompatible"]

    def survivor_positions(self, index) -> list:
        """Позиции, оставшиеся после сужения, — это вход алгоритма D."""
        codes = set(self.survivors)
        return [p for code, p in index.by_code.items() if code in codes]

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "catalog_version": self.catalog_version,
            "pool": self.pool,
            "counts": self.counts,
            "flags": self.flags.to_dict(),
            "selected_ktru_code": self.selected_ktru_code,
            "candidates": [c.to_dict() for c in self.candidates],
            "trace": self.trace,
        }


# ------------------------------------------------------------------ результат D
@dataclass
class Question:
    """Вопрос, который D возвращает интерфейсу."""

    def_id: Optional[str] = None
    name: Optional[str] = None
    reason: str = "splits_candidates"      # splits_candidates | fixes_conflict | confirms_type
    answer_kind: str = "choice"            # choice | number | text | boolean | number_set | text_set
    unit: Optional[Unit] = None
    choices: Optional[list] = None
    bounds: Optional[dict] = None
    candidates_before: int = 0
    buckets: int = 0
    worst_case: int = 0
    alternatives: list = field(default_factory=list)
    stop_reason: Optional[str] = None

    def to_dict(self) -> dict:
        if self.stop_reason:
            return {"question": None, "stop_reason": self.stop_reason, "alternatives": self.alternatives}
        return {
            "question": {
                "def_id": self.def_id,
                "name": self.name,
                "reason": self.reason,
                "answer_kind": self.answer_kind,
                "unit": self.unit.to_dict() if self.unit else None,
                "choices": self.choices,
                "bounds": self.bounds,
                "expected_effect": {
                    "candidates_before": self.candidates_before,
                    "buckets": self.buckets,
                    "worst_case": self.worst_case,
                },
                "fallback": {"on_unknown": "keep_candidates"},
            },
            "alternatives": self.alternatives,
            "stop_reason": None,
        }


# --------------------------------------------------------------- строковый мост
def fact_from_string(text: str, def_id: Optional[str] = None, confidence: float = 0.5) -> Fact:
    """Временный мост, пока модель отдаёт строку: «Мощность: 0,65 кВт».

    Уверенность принудительно ниже порога: такой факт уточняется у пользователя,
    но никогда не исключает код молча.
    """
    text = (text or "").strip()
    name, rest = text, ""
    for sep in (":", "="):
        if sep in text:
            name, rest = text.split(sep, 1)
            break
    else:
        parts = text.rsplit(" ", 1)
        if len(parts) == 2 and any(ch.isdigit() for ch in parts[1]):
            name, rest = parts[0], parts[1]
    name, rest = name.strip(), rest.strip()

    tokens = rest.split()
    num, unit_text = None, None
    if tokens:
        head, last = " ".join(tokens[:-1]), tokens[-1]
        try:
            num, unit_text = float(head.replace(",", ".").replace(" ", "")), last
        except ValueError:
            try:
                num = float(rest.replace(",", ".").replace(" ", ""))
            except ValueError:
                num = None
            if num is None:
                unit_text = last
    if num is not None:
        return Fact(kind="number", value=num, name=name, def_id=def_id, unit=Unit(text=unit_text),
                    extraction_method="free_text", extraction_confidence=min(confidence, 0.5))
    return Fact(kind="text", value=rest or name, name=name, def_id=def_id,
                extraction_method="free_text", extraction_confidence=min(confidence, 0.5))
