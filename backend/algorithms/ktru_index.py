"""Индекс справочника КТРУ из ktru_clean: позиции, семейства, типизированные ограничения.

Используется и AlgorithmC, и AlgorithmD. Строится один раз на старте приложения.
"""
from __future__ import annotations

import gzip
import json
import os
import unicodedata
from dataclasses import dataclass, field
from typing import Iterator, Optional

DEFAULT_CATALOG_VERSION = "ktru_clean_v3.1"


# ------------------------------------------------------------------- значения
@dataclass
class Alternative:
    """Одна допустимая альтернатива значения. Альтернативы одного свойства — «ИЛИ»."""

    kind: str                                    # text | number | range
    text: Optional[str] = None
    number: Optional[float] = None
    lo: Optional[float] = None
    lo_strict: bool = False
    hi: Optional[float] = None
    hi_strict: bool = False
    unit_code: Optional[str] = None
    unit_text: Optional[str] = None
    value_code: Optional[str] = None

    def sort_key(self) -> tuple:
        def num(x):
            return (0, "") if x is None else (1, float(x))
        return (self.kind or "", self.text or "", num(self.number), num(self.lo),
                bool(self.lo_strict), num(self.hi), bool(self.hi_strict), self.unit_code or "")

    def key(self) -> tuple:
        return (self.kind, self.text, self.number, self.lo, self.lo_strict, self.hi, self.hi_strict, self.unit_code)

    def describe(self) -> str:
        unit = (" " + self.unit_text) if self.unit_text else ""
        if self.kind == "text":
            return '"%s"' % self.text
        if self.kind == "number":
            return _fmt(self.number) + unit
        parts = []
        if self.lo is not None:
            parts.append("%s%s" % (">" if self.lo_strict else ">=", _fmt(self.lo)))
        if self.hi is not None:
            parts.append("%s%s" % ("<" if self.hi_strict else "<=", _fmt(self.hi)))
        return " и ".join(parts) + unit


def _fmt(x: Optional[float]) -> str:
    if x is None:
        return "?"
    if float(x) == int(x):
        return str(int(x))
    return ("%.6f" % x).rstrip("0").rstrip(".")


# ------------------------------------------------------------- характеристика
@dataclass
class Characteristic:
    code: Optional[str]
    name: str
    type: str                # "1" качественная, "2" количественная
    kind: str                # "1" неизменяемая, "2" один вариант, "3" несколько
    is_required: bool
    choice_type: Optional[str]
    alternatives: list = field(default_factory=list)

    @property
    def is_quantitative(self) -> bool:
        return self.type == "2"

    def texts(self) -> list:
        return [a.text for a in self.alternatives if a.kind == "text"]

    def unit_codes(self) -> set:
        return {a.unit_code for a in self.alternatives if a.unit_code}


# ------------------------------------------------------------------- позиция
@dataclass
class Position:
    code: str
    name: Optional[str]
    is_template: bool
    status: str
    has_characteristics: bool
    resolve_blocked: bool
    resolve_blocked_reason: Optional[list]
    parent_code: Optional[str]
    application_date_start: Optional[str]
    no_new_features: Optional[bool]
    category: Optional[str]
    characteristics: list = field(default_factory=list)

    def char_by_def_id(self, def_id: Optional[str]) -> Optional[Characteristic]:
        if not def_id:
            return None
        for c in self.characteristics:
            if c.code == def_id:
                return c
        return None

    def chars_by_name(self, name: Optional[str]) -> list:
        """Все характеристики позиции с таким именем (0, 1 или несколько)."""
        if not name:
            return []
        key = normalize_name(name)
        exact = [c for c in self.characteristics if normalize_name(c.name) == key]
        if exact:
            return exact
        return []

    def char_by_name(self, name: Optional[str]) -> Optional[Characteristic]:
        found = self.chars_by_name(name)
        return found[0] if len(found) == 1 else None

    def required_names(self) -> set:
        return {c.name for c in self.characteristics if c.is_required}

    def structure_hash(self) -> tuple:
        """Каноническая структура ограничений: нужна для «позиции неразличимы»."""
        parts = []
        for c in sorted(self.characteristics, key=lambda c: (normalize_name(c.name), c.code or "")):
            parts.append((
                normalize_name(c.name), c.type, c.kind, c.is_required,
                tuple(sorted(a.sort_key() for a in c.alternatives)),
            ))
        return tuple(parts)


# --------------------------------------------------------------- нормализация
def normalize_name(value: Optional[str]) -> str:
    if not value:
        return ""
    s = unicodedata.normalize("NFC", str(value)).lower().replace("ё", "е")
    return " ".join(s.split()).strip(" .,;:")


def normalize_text(value: Optional[str]) -> str:
    """Сравнение качественных значений: регистр, ё/е, пробелы. Пунктуация сохраняется."""
    if value is None:
        return ""
    s = unicodedata.normalize("NFC", str(value)).lower().replace("ё", "е")
    return " ".join(s.split())


# -------------------------------------------------------------------- загрузка
def _to_float(x) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(str(x).replace(",", "."))
    except ValueError:
        return None


def _alternatives(raw_values: list) -> Iterator[Alternative]:
    for v in raw_values or []:
        unit = v.get("okei") or {}
        common = dict(unit_code=unit.get("code"), unit_text=unit.get("name"), value_code=v.get("value_code"))
        if v.get("quality_description") is not None:
            yield Alternative(kind="text", text=v["quality_description"], **common)
        for cv in (v.get("concrete_values") or []):
            num = _to_float(cv)
            if num is not None:
                yield Alternative(kind="number", number=num, **common)
        for r in (v.get("ranges") or []):
            yield Alternative(
                kind="range",
                lo=_to_float(r.get("min")),
                lo_strict=(r.get("min_notation") == "greater"),
                hi=_to_float(r.get("max")),
                hi_strict=(r.get("max_notation") == "less"),
                **common,
            )


def _to_position(raw: dict) -> Position:
    chars = []
    for c in raw.get("characteristics") or []:
        chars.append(Characteristic(
            code=c.get("code"),
            name=c.get("name") or "",
            type=str(c.get("type")),
            kind=str(c.get("kind")),
            is_required=bool(c.get("is_required")),
            choice_type=(str(c["choice_type"]) if c.get("choice_type") is not None else None),
            alternatives=list(_alternatives(c.get("values") or [])),
        ))
    return Position(
        code=raw["code"],
        name=raw.get("name"),
        is_template=bool(raw.get("is_template")),
        status=raw.get("status") or "ACTIVE",
        has_characteristics=bool(raw.get("has_characteristics")),
        resolve_blocked=bool(raw.get("resolve_blocked")),
        resolve_blocked_reason=raw.get("resolve_blocked_reason"),
        parent_code=(raw.get("parent_position") or {}).get("code"),
        application_date_start=raw.get("application_date_start"),
        no_new_features=raw.get("no_new_features"),
        category=raw.get("category"),
        characteristics=chars,
    )


class KtruIndex:
    """Загруженный справочник. Единственный источник ограничений для C и D."""

    def __init__(self, positions: dict, families: dict, roots: list,
                 version: str = DEFAULT_CATALOG_VERSION):
        self.by_code = positions
        self.families = families                 # шаблон -> конечные позиции
        self.roots = roots                       # конечные без родителя
        self.version = version
        self._by_name = {}
        for p in positions.values():
            if not p.is_template:
                self._by_name.setdefault(normalize_name(p.name), []).append(p.code)

    # ------------------------------------------------------------------ загрузка
    @classmethod
    def load(cls, corpus_path: Optional[str] = None, version: str = DEFAULT_CATALOG_VERSION) -> "KtruIndex":
        path = corpus_path or find_corpus()
        positions, families, roots = {}, {}, []
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                p = _to_position(json.loads(line))
                positions[p.code] = p
                if p.parent_code:
                    families.setdefault(p.parent_code, []).append(p)
                else:
                    roots.append(p)
        return cls(positions, families, roots, version)

    @classmethod
    def load_json(cls, path: str) -> "KtruIndex":
        """Загрузка мини-справочника (фикстура без .gz) — для тестов и валидации."""
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        positions, families, roots = {}, {}, []
        for raw in data.get("positions", []):
            p = _to_position(raw)
            positions[p.code] = p
            if p.parent_code:
                families.setdefault(p.parent_code, []).append(p)
            else:
                roots.append(p)
        return cls(positions, families, roots, data.get("version", "fixture"))

    # ------------------------------------------------------------------ выборки
    def pool_for(self, codes) -> list:
        out = []
        for code in codes or []:
            p = self.by_code.get(code)
            if p is None or p.is_template or p.status != "ACTIVE":
                continue
            out.append(p)
        return out

    def family_pool(self, template_code: str) -> list:
        return [p for p in self.families.get(template_code, [])
                if not p.is_template and p.status == "ACTIVE"]

    def find_pool_by_position_name(self, name: str) -> Optional[list]:
        """Черновой поиск семейства по точному названию позиции (заглушка вместо модели B)."""
        codes = self._by_name.get(normalize_name(name)) or []
        for code in codes:
            p = self.by_code[code]
            pool = self.family_pool(p.parent_code) if p.parent_code else [p]
            if pool:
                return pool
        return None

    def stats(self) -> dict:
        finite = [p for p in self.by_code.values() if not p.is_template]
        return {
            "positions": len(self.by_code),
            "templates": sum(1 for p in self.by_code.values() if p.is_template),
            "finite": len(finite),
            "families": len(self.families),
            "roots": len(self.roots),
            "characteristics": sum(len(p.characteristics) for p in self.by_code.values()),
            "alternatives": sum(len(c.alternatives) for p in self.by_code.values() for c in p.characteristics),
            "resolve_blocked": sum(1 for p in self.by_code.values() if p.resolve_blocked),
        }


def find_corpus() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "data", "ktru_actual_all.jsonl.gz"),
        os.path.join(os.path.dirname(here), "data", "ktru_actual_all.jsonl.gz"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    raise FileNotFoundError(
        "не найден ktru_actual_all.jsonl.gz; положите его в algorithms/data/")
