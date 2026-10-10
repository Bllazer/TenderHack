"""Алгоритм C — строгая совместимость и сужение пула конечных кодов КТРУ.

Вход: пул конечных позиций (раскрытый по связям) + типизированные факты от модели.
Выход: вердикт по каждой позиции (compatible / incompatible / unknown), причины
исключения, что осталось непроверенным, набор флагов и трасса сужения.

Исключает код только подтверждённая несовместимость. Неизвестное, несопоставленное
и низкоуверенное не исключают ничего — иначе правильный код теряется молча.

Запуск: run_c(pool, facts, index, type_verified=True)
"""
from __future__ import annotations

import collections
import json
import os
import unicodedata
from typing import Optional

from .contract import (
    Candidate, CResult, Exclusion, Fact, FlagSet, Pool, Unit, Unresolved,
)
from .ktru_index import Characteristic, KtruIndex, Position, normalize_name, normalize_text

TOL = 1e-9
MIN_CONFIDENCE_FOR_HARD_FILTER = 0.6


# =============================================================== единицы ======
class UnitError(Exception):
    """Единицу нельзя применить: неизвестна или не та размерность."""


def _norm_unit(value) -> str:
    s = unicodedata.normalize("NFC", str(value or "")).lower().replace("ё", "е")
    return s.replace(" ", "").replace(".", "").replace("^", "").strip()


#: размерность -> (базовая единица, {написание: множитель к базовой})
DIMENSIONS = {
    "length": ("м", {
        "м": 1, "метр": 1, "mm": 1e-3, "мм": 1e-3, "миллиметр": 1e-3, "см": 1e-2, "сантиметр": 1e-2,
        "дм": 0.1, "км": 1e3, "километр": 1e3, "мкм": 1e-6, "микрометр": 1e-6, "микрон": 1e-6,
        "нм": 1e-9, "дюйм": 0.0254, "дюймы": 0.0254, "in": 0.0254, "фут": 0.3048,
        "пог.м": 1, "погм": 1, "погонныйметр": 1, "п.м": 1, "пм": 1,
    }),
    "mass": ("кг", {
        "кг": 1, "килограмм": 1, "г": 1e-3, "грамм": 1e-3, "гр": 1e-3, "мг": 1e-6, "миллиграмм": 1e-6,
        "мкг": 1e-9, "т": 1e3, "тонна": 1e3, "ц": 100, "центнер": 100,
    }),
    "volume": ("м3", {
        "м3": 1, "кубическийметр": 1, "л": 1e-3, "литр": 1e-3, "мл": 1e-6, "миллилитр": 1e-6,
        "мкл": 1e-9, "микролитр": 1e-9, "дл": 1e-4, "см3": 1e-6, "кубическийсантиметр": 1e-6,
        "дм3": 1e-3, "мм3": 1e-9,
    }),
    "area": ("м2", {
        "м2": 1, "квм": 1, "квадратныйметр": 1, "см2": 1e-4, "квсм": 1e-4, "квадратныйсантиметр": 1e-4,
        "мм2": 1e-6, "квмм": 1e-6, "квадратныймиллиметр": 1e-6, "га": 1e4, "гектар": 1e4,
        "км2": 1e6, "квадратныйкилометр": 1e6,
    }),
    "time": ("с", {
        "с": 1, "сек": 1, "секунда": 1, "мин": 60, "минута": 60, "ч": 3600, "час": 3600,
        "сут": 86400, "сутки": 86400, "нед": 604800, "неделя": 604800, "мес": 2592000, "месяц": 2592000,
        "год": 31536000, "лет": 31536000, "мс": 1e-3, "миллисекунда": 1e-3, "мкс": 1e-6,
    }),
    "angle": ("рад", {
        "рад": 1, "радиан": 1, "град": 0.017453292519943295, "градус": 0.017453292519943295,
        "°": 0.017453292519943295,
    }),
    "speed": ("м/с", {"м/с": 1, "км/ч": 0.2777777777777778, "мм/с": 1e-3, "см/с": 1e-2}),
    "frequency": ("Гц", {"гц": 1, "герц": 1, "кгц": 1e3, "килогерц": 1e3, "мгц": 1e6, "мегагерц": 1e6}),
    "rotation": ("об/мин", {"об/мин": 1, "обмин": 1, "оборотвминуту": 1, "rpm": 1, "мин-1": 1,
                            "1/мин": 1, "об/с": 60, "оборотвсекунду": 60}),
    "pressure": ("Па", {
        "па": 1, "паскаль": 1, "кпа": 1e3, "килопаскаль": 1e3, "мпа": 1e6, "мегапаскаль": 1e6,
        "бар": 1e5, "мбар": 100, "атм": 101325, "атмосфера": 101325,
        "ммрт.ст.": 133.32236842105263, "ммртст": 133.32236842105263,
        "ммвод.ст.": 9.80665, "кгс/см2": 98066.5, "кгс/м2": 9.80665, "psi": 6894.757293168,
    }),
    "temperature": ("°C", {"°c": 1, "c": 1, "градc": 1, "градусцельсия": 1, "цельсий": 1,
                           "[0*]с": 1, "к": 1, "кельвин": 1}),
    "power": ("Вт", {"вт": 1, "ватт": 1, "квт": 1e3, "киловатт": 1e3, "мвт": 1e6, "мегаватт": 1e6,
                     "л.с.": 735.49875, "лс": 735.49875, "лошадинаясила": 735.49875}),
    "voltage": ("В", {"в": 1, "вольт": 1, "кв": 1e3, "киловольт": 1e3, "мв": 1e-3, "милливольт": 1e-3}),
    "current": ("А", {"а": 1, "ампер": 1, "ма": 1e-3, "миллиампер": 1e-3, "ка": 1e3}),
    "charge": ("А·ч", {"а·ч": 1, "ач": 1, "амперчас": 1, "ма·ч": 1e-3, "мач": 1e-3, "mah": 1e-3}),
    "energy": ("Дж", {"дж": 1, "джоуль": 1, "кдж": 1e3, "квт·ч": 3.6e6, "квтч": 3.6e6,
                      "кал": 4.184, "ккал": 4184}),
    "force": ("Н", {"н": 1, "ньютон": 1, "кн": 1e3, "килоньютон": 1e3, "кгс": 9.80665}),
    "torque": ("Н·м", {"н·м": 1, "нм": 1, "ньютонметр": 1, "кгс·м": 9.80665}),
    "data": ("байт", {"байт": 1, "б": 1, "кб": 1e3, "килобайт": 1e3, "мб": 1e6, "мегабайт": 1e6,
                      "гб": 1e9, "гигабайт": 1e9, "тб": 1e12, "терабайт": 1e12, "бит": 0.125}),
    "ratio": ("1", {"%": 0.01, "процент": 0.01, "проц": 0.01, "ppm": 1e-6, "дб": 1, "децибел": 1,
                    "ед": 1, "единица": 1}),
    "count": ("шт", {"шт": 1, "штука": 1, "штук": 1, "штуки": 1, "пар": 2, "пара": 2,
                     "компл": 1, "комплект": 1, "набор": 1, "упак": 1, "упаковка": 1,
                     "тыс": 1000, "тысяча": 1000, "рул": 1, "рулон": 1, "лист": 1}),
    "density_area": ("кг/м2", {"кг/м2": 1, "г/м2": 1e-3, "г/кв.м": 1e-3, "гр/м2": 1e-3}),
    "density_linear": ("кг/м", {"кг/м": 1, "г/м": 1e-3, "текс": 1e-6, "г/км": 1e-6}),
    "concentration": ("мг/м3", {"мг/м3": 1, "мкг/м3": 1e-3, "г/м3": 1e3, "мг/л": 1e3, "г/л": 1e6}),
    "flow": ("м3/ч", {"м3/ч": 1, "л/мин": 0.06, "л/с": 3.6, "мл/мин": 6e-5}),
}

#: температура переводится сдвигом, а не множителем
AFFINE = {("°c", "к"): lambda x: x + 273.15, ("к", "°c"): lambda x: x - 273.15}


class Units:
    """Словарь единиц: коды ОКЕИ, алиасы сокращений, размерности и перевод."""

    def __init__(self):
        self.by_code = {}
        self.aliases = {}
        self.context_required = set()
        self._lookup = {}
        for dim, (base, table) in DIMENSIONS.items():
            for spelling, factor in table.items():
                key = _norm_unit(spelling)
                if key:
                    self._lookup.setdefault((dim, key), (base, factor))

    @classmethod
    def load(cls, data_dir: Optional[str] = None) -> "Units":
        self = cls()
        base = data_dir or find_data_dir()
        okei_path = os.path.join(base, "okei_units.json")
        alias_path = os.path.join(base, "example_unit_strings.json")
        if os.path.exists(okei_path):
            with open(okei_path, encoding="utf-8") as fh:
                for u in json.load(fh).get("units", []):
                    if u.get("code"):
                        self.by_code[str(u["code"])] = u
        if os.path.exists(alias_path):
            with open(alias_path, encoding="utf-8") as fh:
                data = json.load(fh)
            for a in data.get("aliases") or []:
                key = _norm_unit(a.get("raw"))
                if key:
                    self.aliases[key] = a
            self.context_required = {_norm_unit(x) for x in (data.get("context_required") or [])}
        return self

    # ------------------------------------------------------------------ разбор
    def _dimension_of_text(self, key: str):
        exact = collections.defaultdict(set)
        for (dim, spelling), (base, factor) in self._lookup.items():
            if spelling == key:
                exact[dim].add(factor)
        if len(exact) == 1:
            dim = next(iter(exact))
            factors = exact[dim]
            return (dim, next(iter(factors))) if len(factors) == 1 else (None, None)
        if len(exact) > 1:
            return None, None
        long = collections.defaultdict(set)
        for (dim, spelling), (base, factor) in self._lookup.items():
            if len(spelling) >= 4 and spelling in key:
                long[dim].add(factor)
        if len(long) == 1:
            dim = next(iter(long))
            factors = long[dim]
            if len(factors) == 1:
                return dim, next(iter(factors))
        return None, None

    def resolve_code(self, code: Optional[str]):
        if not code:
            return ResolvedUnit(None, None, None, None)
        u = self.by_code.get(str(code)) or self.by_code.get(str(code).zfill(3))
        if not u:
            return ResolvedUnit(str(code), None, None, None)
        dim, factor = self._dimension_of_name(u.get("name"))
        return ResolvedUnit(str(code), u.get("name"), dim, factor)

    def _dimension_of_name(self, name):
        if not name:
            return None, None
        for part in str(name).replace(";", "|").split("|"):
            dim, factor = self._dimension_of_text(_norm_unit(part))
            if dim:
                return dim, factor
        return None, None

    def resolve_text(self, text: Optional[str], global_only: bool = True):
        if not text:
            return ResolvedUnit(None, None, None, None)
        key = _norm_unit(text)
        alias = self.aliases.get(key)
        if alias:
            if global_only and not alias.get("global_safe", True):
                return ResolvedUnit(None, text, None, None, global_safe=False,
                                    note="нужен контекст характеристики")
            dim, factor = self._dimension_of_name(alias.get("okei_name"))
            return ResolvedUnit(alias.get("okei_code"), alias.get("okei_name"), dim, factor,
                                global_safe=alias.get("global_safe", True), note=alias.get("note"))
        dim, factor = self._dimension_of_text(key)
        if dim:
            return ResolvedUnit(None, text, dim, factor, note="распознано по написанию")
        return ResolvedUnit(None, text, None, None, note="единица неизвестна")

    # ------------------------------------------------------------------ перевод
    def convert(self, value: float, src, dst) -> float:
        if dst is None or dst.dimension is None:
            raise UnitError("у целевого значения нет единицы измерения")
        if src is None or src.dimension is None:
            raise UnitError("единица пользователя не распознана")
        if src.dimension != dst.dimension:
            raise UnitError("%s измеряет %s, а нужно %s"
                            % (src.name or src.code, src.dimension, dst.dimension))
        if src.dimension == "temperature":
            key = ((src.name or "").lower()[:1], (dst.name or "").lower()[:1])
            fn = AFFINE.get(key)
            if fn:
                return fn(value)
        f_src = src.factor if src.factor is not None else 1.0
        f_dst = dst.factor if dst.factor is not None else 1.0
        return value * f_src / f_dst


class ResolvedUnit:
    __slots__ = ("code", "name", "dimension", "factor", "global_safe", "note")

    def __init__(self, code, name, dimension, factor, global_safe=True, note=None):
        self.code = code
        self.name = name
        self.dimension = dimension
        self.factor = factor
        self.global_safe = global_safe
        self.note = note

    def __repr__(self):
        return "ResolvedUnit(code=%r, name=%r, dimension=%r)" % (self.code, self.name, self.dimension)


def find_data_dir() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    for c in (os.path.join(here, "data"), os.path.join(os.path.dirname(here), "data")):
        if os.path.exists(os.path.join(c, "okei_units.json")):
            return c
    raise FileNotFoundError("не найден okei_units.json; положите словари в algorithms2/data/")


# ============================================================ проверка ========
def value_in_range(value: float, alt) -> bool:
    if alt.lo is not None:
        if value < alt.lo - TOL or (abs(value - alt.lo) <= TOL and alt.lo_strict):
            return False
    if alt.hi is not None:
        if value > alt.hi + TOL or (abs(value - alt.hi) <= TOL and alt.hi_strict):
            return False
    return not (alt.lo is None and alt.hi is None)


def range_inside(lo, hi, alt) -> bool:
    """Пользовательский интервал целиком внутри каталожного; частичное пересечение — не подтверждение."""
    if lo is None and hi is None:
        return False
    if lo is not None:
        if alt.lo is None or lo < alt.lo - TOL or (abs(lo - alt.lo) <= TOL and alt.lo_strict):
            return False
    if hi is not None:
        if alt.hi is None or hi > alt.hi + TOL or (abs(hi - alt.hi) <= TOL and alt.hi_strict):
            return False
    return True


def to_target_unit(units: Units, char: Characteristic, value: float, fact: Fact):
    """Приводит число факта к единице характеристики. Возвращает (значение, причина_неизвестности)."""
    targets = [a for a in char.alternatives if a.unit_code]
    src = units.resolve_code(fact.unit.code) if (fact.unit and fact.unit.code) else \
        units.resolve_text(fact.unit.text) if fact.unit else ResolvedUnit(None, None, None, None)
    if src.dimension is None and fact.unit and fact.unit.text:
        src = units.resolve_text(fact.unit.text)

    if not targets:
        # единицы у характеристики нет: принимаем число как есть, но единица факта не должна
        # быть из другой физической величины — «5 кг» для длины не доказательство
        if src.dimension not in (None, "count", "ratio", "length"):
            return None, "unit_dimension_unsupported"
        return value, None

    if not fact.unit or not (fact.unit.code or fact.unit.text):
        return None, "user_unit_missing"          # «10» не превращаем в 10 мм

    if src.dimension is None:
        return None, "user_unit_unknown"

    def target_unit(alt):
        t = units.resolve_code(alt.unit_code)
        if t.dimension is None and alt.unit_text:
            t = units.resolve_text(alt.unit_text)
        return t

    dims = {target_unit(a).dimension for a in targets}
    if len(dims) > 1:
        return None, "value_units_ambiguous"      # альтернативы в разных единицах
    dst = target_unit(targets[0])
    if dst.dimension is None:
        return None, "position_unit_unknown"
    try:
        return units.convert(value, src, dst), None
    except UnitError:
        return None, "unit_dimension_unsupported"


def check_fact(units: Units, char: Characteristic, fact: Fact):
    """Один факт против одной характеристики. Возвращает (verdict, detail)."""
    if not char.alternatives:
        return "unknown", {"reason": "position_has_no_allowed_values"}
    if fact.polarity == "uncertain":
        return "unknown", {"reason": "uncertain_assertion"}
    if fact.polarity == "negated":
        allowed = [a for a in char.alternatives if a.kind == "text"]
        if allowed:
            if any(normalize_text(a.text) in ("нет", "отсутствует", "не предусмотрено") for a in allowed):
                return "compatible", None
            return "unknown", {"reason": "negation_not_supported"}
        return "unknown", {"reason": "negation_not_supported"}

    kind = fact.kind

    if kind == "number" and isinstance(fact.value, (int, float)) and not isinstance(fact.value, bool):
        value, why = to_target_unit(units, char, float(fact.value), fact)
        if why:
            return "unknown", {"reason": why}
        for a in char.alternatives:
            if a.kind == "number" and a.number is not None and abs(a.number - value) <= max(TOL, abs(a.number) * 1e-9):
                return "compatible", None
            if a.kind == "range" and value_in_range(value, a):
                return "compatible", None
        return "incompatible", {"reason": "out_of_range_or_set", "value": value}

    if kind == "range" and isinstance(fact.value, dict):
        lo, hi = fact.value.get("min"), fact.value.get("max")
        lo = float(lo) if lo is not None else None
        hi = float(hi) if hi is not None else None
        if not [a for a in char.alternatives if a.kind in ("number", "range")]:
            return "unknown", {"reason": "range_against_text_characteristic"}
        if lo is not None:
            lo, why = to_target_unit(units, char, lo, fact)
            if why:
                return "unknown", {"reason": why}
        if hi is not None:
            hi, why = to_target_unit(units, char, hi, fact)
            if why:
                return "unknown", {"reason": why}
        for a in char.alternatives:
            if a.kind == "number" and a.number is not None and lo is not None and hi is not None \
                    and lo - TOL <= a.number <= hi + TOL:
                return "compatible", None
            if a.kind == "range" and range_inside(lo, hi, a):
                return "compatible", None
        return "incompatible", {"reason": "user_range_not_inside_catalogue_range"}

    if kind == "number_set":
        subs = [check_fact(units, char, Fact(kind="number", value=v, unit=fact.unit,
                                             polarity=fact.polarity,
                                             extraction_confidence=fact.extraction_confidence))
                for v in (fact.value or [])]
        verdicts = [v for v, _ in subs]
        if verdicts and all(v == "compatible" for v in verdicts):
            return "compatible", None
        if any(v == "incompatible" for v in verdicts):
            return "incompatible", {"reason": "some_values_not_allowed"}
        return "unknown", {"reason": "some_values_unchecked"}

    if kind in ("text", "text_set", "boolean"):
        allowed = [a.text for a in char.alternatives if a.kind == "text"]
        if not allowed:
            return "unknown", {"reason": "text_against_numeric_characteristic"}
        if kind == "boolean":
            values = ["да" if fact.value else "нет"]
        elif kind == "text_set":
            values = list(fact.value or [])
        else:
            values = [fact.value]
        norm_allowed = {normalize_text(a) for a in allowed}
        marks = []
        for v in values:
            nv = normalize_text(v)
            if nv in norm_allowed:
                marks.append("compatible")
                continue
            try:                                   # число там, где КТРУ держит его текстом
                fv = float(str(v).replace(",", "."))
                if any(_same_number_text(fv, a) for a in allowed):
                    marks.append("compatible")
                    continue
            except (TypeError, ValueError):
                pass
            marks.append("incompatible")
        if marks and all(m == "compatible" for m in marks):
            return "compatible", None
        if any(m == "incompatible" for m in marks):
            return "incompatible", {"reason": "not_in_allowed_values", "allowed": allowed[:12]}
        return "unknown", {"reason": "unchecked_text"}

    return "unknown", {"reason": "unsupported_value_shape"}


def _same_number_text(x: float, text: str) -> bool:
    try:
        return abs(float(str(text).replace(",", ".")) - x) <= TOL
    except (TypeError, ValueError):
        return False


def describe_rule(char: Characteristic) -> str:
    if not char.alternatives:
        return "допустимые значения не заданы"
    shown = ", ".join(a.describe() for a in char.alternatives[:6])
    more = "" if len(char.alternatives) <= 6 else " (+%d)" % (len(char.alternatives) - 6)
    return "допустимо: %s%s" % (shown, more)


def describe_fact(fact: Fact) -> str:
    if fact.kind == "range" and isinstance(fact.value, dict):
        body = "%s…%s" % (fact.value.get("min"), fact.value.get("max"))
    elif fact.kind in ("number_set", "text_set"):
        body = ", ".join(str(v) for v in (fact.value or []))
    else:
        body = str(fact.value)
    unit = (" " + fact.unit.text) if (fact.unit and fact.unit.text) else ""
    return "%s%s%s" % (body, unit, (" " + fact.operator) if fact.operator else "")


# ================================================================ запуск ======
def run_c(pool, facts, index: KtruIndex, units: Optional[Units] = None, *,
          type_verified: bool = False, scenario_date: str = "2026-10-09",
          catalog_scope_complete: bool = True, max_candidates: int = 50) -> CResult:
    """Основная функция алгоритма C.

    pool   — список Position (пул конечных кодов, раскрытый по связям)
    facts  — список Fact (от модели, после сопоставления свойств)
    index  — справочник КТРУ

    Позиции с resolve_blocked (шаблоны, позиции без характеристик, дефекты источника)
    исключаются из сужения и возвращаются отдельным счётчиком: они не могут получить
    итоговый код, но и не должны мешать выбору среди остальных.
    """
    units = units or Units.load()
    evidence, soft = [], []
    for f in facts:
        problems = f.problems()
        if problems:
            soft.append((f, "invalid_format: " + "; ".join(problems)))
            continue
        if not f.is_hard:
            soft.append((f, "low_confidence"))
        evidence.append(f)
    soft_ids = {f.fact_id for f, _ in soft if f.fact_id}

    blocked = [p.code for p in pool if p.resolve_blocked]
    usable = [p for p in pool if not p.resolve_blocked]

    candidates = []
    checked_counts = {}
    for p in usable:
        candidate, stats = _check_position(p, evidence, units, scenario_date, soft_ids)
        candidates.append(candidate)
        checked_counts[p.code] = stats

    trace = []
    for fact in evidence:
        touched = [c for c in candidates if not any(e.fact_id == fact.fact_id for e in c.exclusions)]
        trace.append({
            "fact_id": fact.fact_id,
            "property": fact.name,
            "def_id": fact.def_id,
            "before": len(candidates),
            "after": len(touched),
            "hard": fact.is_hard,
        })

    survivors = [c for c in candidates if c.verdict != "incompatible"]
    flags = _build_flags(candidates, survivors, evidence, soft, type_verified,
                         catalog_scope_complete, checked_counts)
    selected = survivors[0].code if (len(survivors) == 1 and flags.can_resolve()) else None

    ordered = sorted(candidates, key=lambda c: (c.verdict == "incompatible", c.code))
    return CResult(
        catalog_version=index.version,
        pool=[p.code for p in pool],
        counts={
            "before": len(usable),
            "compatible_after": sum(1 for c in candidates if c.verdict == "compatible"),
            "unknown_only": sum(1 for c in candidates if c.verdict == "unknown"),
            "incompatible": sum(1 for c in candidates if c.verdict == "incompatible"),
            "blocked_excluded": len(blocked),
        },
        flags=flags,
        selected_ktru_code=selected,
        candidates=ordered if max_candidates <= 0 else ordered[:max_candidates],
        trace=trace,
    )


def _check_position(position: Position, facts: list, units: Units, scenario_date: str,
                    soft_ids: set = frozenset()):
    """Возвращает (Candidate, статистика проверки по позиции).

    Факты из soft_ids (низкая уверенность, неполный формат) проверяются, но НЕ исключают
    позицию: они идут в unresolved с причиной low_confidence_fact, а решение принимает
    пользователь или повторное извлечение.
    """
    exclusions, unresolved = [], []
    checked = supported = matched = 0

    if not position.resolve_blocked:
        for fact in facts:
            char = position.char_by_def_id(fact.def_id) if fact.def_id else None
            if char is None:
                same = position.chars_by_name(fact.name)
                if len(same) > 1:
                    # одно имя — разные характеристики с разными правилами: подставлять
                    # произвольное правило нельзя, иначе позиция исключается по чужому условию
                    unresolved.append(Unresolved(fact.fact_id, fact.name, "name_ambiguous_in_position"))
                    continue
                char = same[0] if same else None
            if char is None:
                other = None
                for alt_id in fact.def_ids()[1:]:
                    other = position.char_by_def_id(alt_id)
                    if other is not None:
                        break
                unresolved.append(Unresolved(fact.fact_id, fact.name,
                                             "matched_other_definition" if other else "not_defined_in_position"))
                continue
            checked += 1
            if char.alternatives:
                supported += 1
            verdict, detail = check_fact(units, char, fact)
            if verdict == "compatible":
                matched += 1
            elif verdict == "incompatible":
                if fact.fact_id in soft_ids:
                    unresolved.append(Unresolved(fact.fact_id, char.name, "low_confidence_fact"))
                else:
                    exclusions.append(Exclusion(
                        fact_id=fact.fact_id, property=char.name, actual=describe_fact(fact),
                        rule=describe_rule(char), def_id=char.code, position=position.code))
            else:
                unresolved.append(Unresolved(fact.fact_id, char.name, (detail or {}).get("reason", "unknown")))

    missing = _missing_required(position, facts)
    future = bool(position.application_date_start
                  and position.application_date_start[:10] > scenario_date)

    if exclusions:
        verdict = "incompatible"
    elif matched == 0:
        # ни один факт не подтвердился: позиция не проверена, а не совместима
        verdict = "unknown"
    elif supported == 0:
        verdict = "unknown"
    else:
        verdict = "compatible"

    candidate = Candidate(
        code=position.code, verdict=verdict, exclusions=exclusions, unresolved=unresolved,
        missing_required=missing, resolve_blocked=position.resolve_blocked,
        resolve_blocked_reason=position.resolve_blocked_reason, future=future)
    return candidate, {"checked": checked, "supported": supported, "matched": matched,
                       "structure_hash": position.structure_hash()}


def _missing_required(position: Position, facts: list) -> list:
    answered = set()
    for f in facts:
        char = position.char_by_def_id(f.def_id) if f.def_id else None
        if char is None:
            same = position.chars_by_name(f.name)
            if len(same) == 1:
                char = same[0]
            elif len(same) > 1:
                # имя неоднозначно, но свойство с таким именем в позиции есть
                answered.update(c.name for c in same)
                continue
        if char is not None:
            answered.add(char.name)
    return sorted(n for n in position.required_names() if n not in answered)


def _facts_conflict(facts: list) -> bool:
    """Противоречат ли факты друг другу.

    Противоречие — это разные значения ОДНОГО И ТОГО ЖЕ свойства: сравниваем по паре
    (сопоставленное определение, нормализованное имя). Разные имена, случайно ведущие
    к одному определению («Вес» и «Масса»), конфликтом не считаются.
    """
    grouped = {}
    for f in facts:
        if f.polarity != "affirmed":
            continue
        key = (f.def_id or "", normalize_name(f.name))
        grouped.setdefault(key, []).append(f)
    for group in grouped.values():
        if len(group) > 1 and len({describe_fact(f).strip().lower() for f in group}) > 1:
            return True
    return False


def _build_flags(candidates, survivors, evidence, soft, type_verified,
                 scope_complete, stats_by_code) -> FlagSet:
    scope = survivors or candidates
    blocking = []
    if _facts_conflict(evidence):
        blocking.append("conflicting_facts")
    if any(c.resolve_blocked for c in scope):
        blocking.append("resolve_blocked")
    if any(u.reason in ("unit_dimension_unsupported", "unsupported_value_shape", "value_units_ambiguous")
           for c in scope for u in c.unresolved if u.reason != "not_defined_in_position"):
        blocking.append("unsupported_constraint")
    # мягкий (низкоуверенный) факт мешает только если он реально кого-то исключил
    soft_ids = {f.fact_id for f, _ in soft if f.fact_id}
    if any(e.fact_id in soft_ids for c in candidates for e in c.exclusions):
        blocking.append("low_confidence_hard_filter")

    value_verified = bool(
        survivors
        and any(stats_by_code.get(c.code, {}).get("supported") for c in survivors)
        and any(stats_by_code.get(c.code, {}).get("checked") for c in survivors)
        and not any(c.exclusions for c in survivors)
        and not any(u.reason in ("unit_dimension_unsupported", "unsupported_value_shape")
                    for c in survivors for u in c.unresolved)
    )
    return FlagSet(
        type_verified=bool(type_verified),
        ktru_unique=len(survivors) == 1,
        fields_complete=bool(survivors) and all(not c.missing_required for c in survivors),
        value_verified=value_verified,
        catalog_scope_complete=bool(scope_complete),
        value_indistinguishable=_indistinguishable(survivors, stats_by_code),
        blocking=sorted(set(blocking)),
    )


def _indistinguishable(survivors, stats_by_code) -> bool:
    """Все выжившие имеют одинаковую структуру ограничений — значениями их не различить."""
    hashes = {stats_by_code[c.code].get("structure_hash") for c in survivors}
    return len(survivors) > 1 and len(hashes) == 1 and None not in hashes
