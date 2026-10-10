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
import math
import os
import unicodedata
from decimal import Decimal
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
        "cm": 1e-2, "m": 1, "дм": 0.1, "км": 1e3, "километр": 1e3, "мкм": 1e-6, "микрометр": 1e-6, "микрон": 1e-6,
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
                           "[0*]с": 1, "к": 1, "кельвин": 1, "k": 1, "°k": 1}),
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
    "ratio": ("1", {
        "ед": 1, "единица": 1, "": 1,
    }),
    # проценты — своя размерность: совместны только с безразмерными долями, но не с дБ
    "percent": ("%", {"%": 1, "процент": 1, "проц": 1, "ppm": 1e-4}),
    # дБ — логарифмическая величина: универсального перевода в доли не существует
    "level": ("дБ", {"дб": 1, "децибел": 1}),
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
            a, b = _temp_kind(src), _temp_kind(dst)
            if a == b:
                return value
            if a == "c" and b == "k":
                return float(Decimal(str(value)) + Decimal("273.15"))
            if a == "k" and b == "c":
                return float(Decimal(str(value)) - Decimal("273.15"))
        f_src = src.factor if src.factor is not None else 1.0
        f_dst = dst.factor if dst.factor is not None else 1.0
        # Точный десятичный перевод: 0,7 м — это ровно 700 мм, а не 700.0000000000001.
        # Иначе допуск размывает строгую границу, и значение, стоящее на открытой
        # границе, проходит как допустимое (или наоборот).
        return float(Decimal(str(value)) * Decimal(str(f_src)) / Decimal(str(f_dst)))


def _temp_kind(u) -> str:
    """Различает шкалу температуры: цельсий или кельвин (по коду ОКЕИ и написанию)."""
    code = str(getattr(u, "code", "") or "")
    if code == "280":
        return "c"
    text = _norm_unit(getattr(u, "name", None) or getattr(u, "text", None) or "")
    if "цельс" in text or "°c" in text or "градc" in text or text in ("c", "[0*]с") or text.startswith("°"):
        return "c"
    return "k"


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
    raise FileNotFoundError("не найден okei_units.json; положите словари в algorithms/data/")


# ============================================================ проверка ========
def value_in_range(value: float, alt) -> bool:
    if alt.lo is not None:
        if value < alt.lo - TOL or (abs(value - alt.lo) <= TOL and alt.lo_strict):
            return False
    if alt.hi is not None:
        if value > alt.hi + TOL or (abs(value - alt.hi) <= TOL and alt.hi_strict):
            return False
    return not (alt.lo is None and alt.hi is None)


# ------------------------------------------------------- покрытие диапазонов
INF = float("inf")


def _norm_bounds(lo, hi, lo_strict=False, hi_strict=False):
    """Границы в каноническом виде: None -> бесконечность; строгость только на конечной границе."""
    lo_v = -INF if lo is None else float(lo)
    hi_v = INF if hi is None else float(hi)
    lo_s = bool(lo_strict) and lo is not None
    hi_s = bool(hi_strict) and hi is not None
    return lo_v, hi_v, lo_s, hi_s


def _alt_segments(alt):
    """Допустимое множество одной альтернативы как список отрезков."""
    if alt.kind == "range" and not (alt.lo is None and alt.hi is None):
        return [_norm_bounds(alt.lo, alt.hi, alt.lo_strict, alt.hi_strict)]
    if alt.kind == "number" and alt.number is not None:
        x = float(alt.number)
        return [(x, x, False, False)]
    return []


def _valid_interval(lo, hi, lo_strict=False, hi_strict=False) -> bool:
    """Непустой и непротиворечивый интервал.

    Односторонний интервал допустим. Перевёрнутый (lo > hi) и пустой (lo == hi при
    строгой границе хотя бы с одной стороны) — нет: по такому ограничению нельзя
    ничего доказать, поэтому проверка обязана вернуть unknown, а не «подходит».
    """
    if lo is None and hi is None:
        return False
    for x in (lo, hi):
        if x is not None and (isinstance(x, bool) or not isinstance(x, (int, float))
                              or not math.isfinite(float(x))):
            return False
    if lo is None or hi is None:
        return True
    if float(lo) > float(hi):
        return False
    return not (float(lo) == float(hi) and (bool(lo_strict) or bool(hi_strict)))


def _valid_alt(alt) -> bool:
    """Годится ли альтернатива-диапазон как ограничение."""
    if alt.kind != "range":
        return True
    return _valid_interval(alt.lo, alt.hi, bool(alt.lo_strict), bool(alt.hi_strict))


def _merge_segments(segments: list) -> list:
    """Слить пересекающиеся и соприкасающиеся отрезки одной единичной группы.

    Соприкосновение дырой не считается, только если обе границы в этой точке
    замкнуты: «[0;2] и [2;5]» — сплошное покрытие, «[0;2) и (2;5]» — дыра в точке 2,
    и требование, включающее эту точку, покрытым не является.
    """
    merged: list = []
    for lo, hi, lo_s, hi_s in sorted(segments):
        if merged:
            plo, phi, plo_s, phi_s = merged[-1]
            if lo < phi or (lo == phi and not (lo_s and phi_s)):
                if lo == plo:
                    plo_s = plo_s and lo_s
                if hi > phi:
                    phi, phi_s = hi, hi_s
                elif hi == phi:
                    phi_s = phi_s and hi_s
                merged[-1] = (plo, phi, plo_s, phi_s)
                continue
        merged.append((lo, hi, lo_s, hi_s))
    return merged


def _segment_contains(ulo, uhi, ulos, uhis, segment) -> bool:
    """Лежит ли множество пользователя целиком внутри одного отрезка допустимых.

    Границы канонические: None заменён бесконечностью. Одностороннее требование не
    лежит внутри ограниченного отрезка («не менее 5» не подтверждается [0;10]), а
    требование, включающее точку на строгой границе отрезка, не подтверждается
    отрезком, который её исключает.
    """
    slo, shi, sls, shs = segment
    if ulo < slo or (ulo == slo and not ulos and sls):
        return False
    if uhi > shi or (uhi == shi and not uhis and shs):
        return False
    return True


def segments_cover(user_lo, user_hi, user_lo_strict, user_hi_strict, segments) -> bool:
    """Полное покрытие требования объединением допустимых отрезков одной группы."""
    if not segments:
        return False
    ulo, uhi, ulos, uhis = _norm_bounds(user_lo, user_hi, user_lo_strict, user_hi_strict)
    return any(_segment_contains(ulo, uhi, ulos, uhis, seg)
               for seg in _merge_segments(segments))


def _convert_point(units: Units, src, dst, value: float):
    """Перевод точки в единицу альтернативы. Возвращает (значение, причина)."""
    if dst is None or dst.dimension is None:
        if src is None or src.dimension is None:
            return value, None
        if src.dimension in ("count", "ratio", "percent"):
            return value, None
        return None, "position_unit_unknown"
    if src is None or src.dimension is None:
        return None, "user_unit_unknown"
    try:
        return units.convert(value, src, dst), None
    except UnitError:
        return None, "unit_dimension_unsupported"


class _Resolution:
    """Итог разбора: либо значение, либо причина, по которой проверить нельзя."""

    def __init__(self, value=None, reason=None, unit_group=None):
        self.value = value
        self.reason = reason
        self.unit_group = unit_group

    def __repr__(self):
        return "Resolved(%r,%r)" % (self.value, self.reason)


def _resolve_number(units: Units, char: Characteristic, value: float, fact: Fact) -> _Resolution:
    """Число факта -> единицы альтернатив.

    Строка «unit_group» описывает, в каких единицах получены значения; если альтернативы
    живут в разных единицах, значение относится только к своей группе.
    """
    alts = [a for a in char.alternatives if a.unit_code or a.unit_text or a.kind == "number" or a.kind == "range"]
    with_unit = [a for a in alts if a.unit_code or a.unit_text]
    if not with_unit:
        if fact.unit and (fact.unit.code or fact.unit.text):
            src = units.resolve_code(fact.unit.code) if fact.unit.code else units.resolve_text(fact.unit.text)
            if src.dimension is None and fact.unit.text:
                src = units.resolve_text(fact.unit.text)
            if src.dimension not in (None, "count", "ratio", "percent", "length"):
                return _Resolution(None, "unit_dimension_unsupported")
        return _Resolution(value, None, None)

    if not fact.unit or not (fact.unit.code or fact.unit.text):
        return _Resolution(None, "user_unit_missing", None)

    src = units.resolve_code(fact.unit.code) if fact.unit.code else units.resolve_text(fact.unit.text)
    if src.dimension is None and fact.unit.text:
        src = units.resolve_text(fact.unit.text)
    if src.dimension is None:
        return _Resolution(None, "user_unit_unknown", None)

    groups = {}
    order = []
    for a in with_unit:
        dst = units.resolve_code(a.unit_code) if a.unit_code else None
        if dst is None or dst.dimension is None:
            dst = units.resolve_text(a.unit_text) if a.unit_text else None
        key = ("dim:" + dst.dimension) if (dst is not None and dst.dimension) else "unknown:" + str(a.unit_text)
        groups.setdefault(key, []).append((a, dst))
        if key not in order:
            order.append(key)

    if len(order) > 1:
        # альтернативы в разных единицах: значение относится к одной группе,
        # остальные проверить сырым числом нельзя
        first = groups[order[0]][0][1]
        if first is None or first.dimension is None or src.dimension != first.dimension:
            return _Resolution(None, "value_units_ambiguous", None)
        try:
            return _Resolution(units.convert(value, src, first), None, order[0])
        except UnitError:
            return _Resolution(None, "value_units_ambiguous", None)

    a, dst = groups[order[0]][0]
    out, why = _convert_point(units, src, dst, value)
    if why:
        return _Resolution(None, why, None)
    return _Resolution(out, None, order[0])


def check_number(units: Units, char: Characteristic, value: float, fact: Fact):
    """Проверка числового требования. Возвращает (verdict, reason).

    Значение приводится к единице КАЖДОЙ альтернативы отдельно: сравнивать сырое число
    с числом альтернативы в другой единице нельзя (для «1 мм OR 2 см» значение 2 мм
    не равно ни 1 мм, ни 20 мм).
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
        return "unknown", "non_finite_value"
    value = float(value)

    op = fact.operator
    if op not in (None, "="):
        # >=, >, <=, <, != — факт задаёт границу требования, а не измеренное значение.
        # Подставив границу как точку, мы бы подтвердили код по значению, которого
        # пользователь не называл, поэтому такое требование остаётся непроверенным.
        if not fact.unit or not (fact.unit.code or fact.unit.text):
            return "unknown", "user_unit_missing"
        return "unknown", "operator_not_confirmable"

    src = None
    if fact.unit and (fact.unit.code or fact.unit.text):
        src = units.resolve_code(fact.unit.code) if fact.unit.code else units.resolve_text(fact.unit.text)
        if src.dimension is None and fact.unit.text:
            src = units.resolve_text(fact.unit.text)

    numeric = [a for a in char.alternatives if a.kind in ("number", "range")]
    if not numeric:
        return "unknown", "text_against_numeric_characteristic"

    with_unit = [a for a in numeric if a.unit_code or a.unit_text]
    if with_unit and (src is None or src.dimension is None):
        if not fact.unit or not (fact.unit.code or fact.unit.text):
            return "unknown", "user_unit_missing"
        return "unknown", "user_unit_unknown"
    if not with_unit and fact.unit and (fact.unit.code or fact.unit.text):
        if src is not None and src.dimension not in (None, "count", "ratio", "percent", "length"):
            return "unknown", "unit_dimension_unsupported"

    unknown_reason = None
    checked_any = False
    for alt in numeric:
        if alt.unit_code or alt.unit_text:
            dst = units.resolve_code(alt.unit_code) if alt.unit_code else None
            if dst is None or dst.dimension is None:
                dst = units.resolve_text(alt.unit_text) if alt.unit_text else None
        else:
            dst = None
        converted, why = _convert_point(units, src, dst, value)
        if why:
            if why in ("user_unit_missing", "user_unit_unknown", "unit_dimension_unsupported"):
                return "unknown", why
            unknown_reason = why
            continue
        checked_any = True
        if alt.kind == "number" and alt.number is not None and abs(float(alt.number) - converted) <= max(TOL, abs(float(alt.number)) * 1e-9):
            return "compatible", None
        if alt.kind == "range" and value_in_range(converted, alt):
            return "compatible", None
    if not checked_any:
        return "unknown", unknown_reason or "position_unit_unknown"
    return "incompatible", "out_of_range_or_set"


def _alt_unit(units: Units, alt):
    """Единица альтернативы как ResolvedUnit или None, если единицы у неё нет."""
    if alt.unit_code:
        dst = units.resolve_code(alt.unit_code)
        if dst is not None and dst.dimension:
            return dst
    if alt.unit_text:
        dst = units.resolve_text(alt.unit_text)
        if dst is not None and dst.dimension:
            return dst
    return None


def _canonical_unit(dimension: str):
    """Каноническая единица размерности: базовая единица таблицы с множителем 1.

    Для температуры базовая единица — °C, поэтому сдвиг учитывается: 300 K при
    приведении даёт 26,85 °C, а не 300 °C. Без этого альтернативы «[0;10] °C» и
    «[300;310] K» складывались бы как сырые числа разных шкал.
    """
    base = DIMENSIONS.get(dimension)
    if not base:
        return None
    return ResolvedUnit(None, base[0], dimension, 1.0)


def _to_canonical(units: Units, source, target, value):
    """Перевести значение в каноническую единицу. Возвращает (значение, причина)."""
    if value is None:
        return None, None
    try:
        return units.convert(float(value), source, target), None
    except UnitError:
        return None, "unit_dimension_unsupported"


def _canonical_segment(units: Units, alt, alt_unit, canonical):
    """Отрезок альтернативы в канонической единице. Возвращает (отрезок, причина).

    Строгость границ переносится как есть: множитель и сдвиг — монотонные
    преобразования, они не меняют открытость границы.
    """
    if alt.kind == "number" and alt.number is not None:
        value, why = _to_canonical(units, alt_unit, canonical, alt.number)
        if why:
            return None, why
        return (value, value, False, False), None
    if alt.kind != "range":
        return None, None
    lo, why_lo = _to_canonical(units, alt_unit, canonical, alt.lo)
    hi, why_hi = _to_canonical(units, alt_unit, canonical, alt.hi)
    why = why_lo or why_hi
    if why:
        return None, why
    return _norm_bounds(lo, hi, alt.lo_strict, alt.hi_strict), None


def _operator_regions(value, op, unit_group):
    """Область, задаваемая оператором, как список отрезков в единицах unit_group."""
    if op == ">=":
        return [(value, INF, False, False)]
    if op == ">":
        return [(value, INF, True, False)]
    if op == "<=":
        return [(-INF, value, False, False)]
    if op == "<":
        return [(-INF, value, False, True)]
    return []


def check_range(units: Units, char: Characteristic, lo, hi, fact: Fact,
                lo_strict: bool = False, hi_strict: bool = False):
    """Проверка диапазона, введённого пользователем.

    Строгость границ требования приходит из fact.value: min_inclusive/max_inclusive
    (та же нотация, что в контракте и в индексе). Строгая граница обязана остаться
    строгой при сравнении с альтернативой, иначе значение, стоящее ровно на открытой
    границе, подтвердит код.
    """
    if lo is not None and not _finite(lo):
        return "unknown", "non_finite_value"
    if hi is not None and not _finite(hi):
        return "unknown", "non_finite_value"
    if lo is None and hi is None:
        return "unknown", "empty_user_range"
    if lo is not None and hi is not None and float(lo) > float(hi):
        return "unknown", "reversed_user_range"          # границы не переставляем молча
    if not _valid_interval(lo, hi, lo_strict, hi_strict):
        return "unknown", "empty_user_range"             # точка при строгой границе пуста

    numeric = [a for a in char.alternatives if a.kind in ("number", "range")]
    if not numeric:
        return "unknown", "range_against_text_characteristic"

    # Единица требования.
    src = None
    if fact.unit and (fact.unit.code or fact.unit.text):
        src = units.resolve_code(fact.unit.code) if fact.unit.code else units.resolve_text(fact.unit.text)
        if src.dimension is None and fact.unit.text:
            src = units.resolve_text(fact.unit.text)

    # Все числовые альтернативы должны жить в ОДНОЙ размерности: только тогда их
    # можно свести к общей единице и объединить. Сырые числа разных единиц (или
    # безразмерное рядом с размерным) складывать нельзя.
    alt_units = [(a, _alt_unit(units, a)) for a in numeric]
    dimensions = {unit.dimension for _, unit in alt_units if unit is not None}
    has_unit = any(unit is not None for _, unit in alt_units)
    has_bare = any(unit is None for _, unit in alt_units)
    if has_unit and has_bare:
        return "unknown", "value_units_ambiguous"
    if len(dimensions) > 1:
        return "unknown", "value_units_ambiguous"

    unknown: list = []
    if not has_unit:
        # Безразмерные альтернативы: значения сравниваются как есть, но требование,
        # выраженное в единице измерения, с безразмерной характеристикой несопоставимо.
        if fact.unit and (fact.unit.code or fact.unit.text):
            if src is None or src.dimension is None:
                return "unknown", "user_unit_unknown"
            if src.dimension not in (None, "count", "ratio", "percent", "length"):
                return "unknown", "unit_dimension_unsupported"
        canonical = None
        user_lo, user_hi = lo, hi
        segments: list = [seg for alt, _ in alt_units for seg in _alt_segments(alt)]
    else:
        canonical = _canonical_unit(next(iter(dimensions)))
        if canonical is None:
            return "unknown", "position_unit_unknown"
        if src is None or src.dimension is None:
            return "unknown", ("user_unit_missing"
                               if not (fact.unit and (fact.unit.code or fact.unit.text))
                               else "user_unit_unknown")
        user_lo, why_lo = _to_canonical(units, src, canonical, lo)
        user_hi, why_hi = _to_canonical(units, src, canonical, hi)
        why = why_lo or why_hi
        if why:
            return "unknown", why
        # Каждая альтернатива приводится к общей единице ДО объединения, строгость
        # сохраняется. Затем проверяется объединение всех альтернатив, а не каждая
        # группа по отдельности: [0;1] см и [10;30] мм вместе покрывают [5;25] мм.
        segments = []
        for alt, alt_unit in alt_units:
            segment, segment_why = _canonical_segment(units, alt, alt_unit, canonical)
            if segment_why:
                unknown.append(segment_why)
                continue
            if segment is not None:
                segments.append(segment)

    if segments_cover(user_lo, user_hi, lo_strict, hi_strict, segments):
        return "compatible", None
    if unknown:
        return "unknown", unknown[0]
    return "incompatible", "user_range_not_covered"


def _finite(x) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def check_fact(units: Units, char: Characteristic, fact: Fact):
    """Один факт против одной характеристики. Возвращает (verdict, detail)."""
    if not char.alternatives:
        return "unknown", {"reason": "position_has_no_allowed_values"}
    if fact.problems():
        return "unknown", {"reason": "invalid_format"}
    if str(char.kind) not in ("1", "2"):
        # несколько допустимых значений — это не одно правило: сравнивать не с чем
        return "unknown", {"reason": "multiple_choice_not_supported"}
    if any(not _valid_alt(a) for a in char.alternatives):
        # пустой или перевёрнутый интервал в самом справочнике: правило недостоверно
        # ЦЕЛИКОМ, и соседняя исправная альтернатива (OR) его не «спасает»
        return "unknown", {"reason": "invalid_catalog_interval"}
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

    if kind == "number" and fact.operator in (">", ">=", "<", "<="):
        # Оператор задаёт ГРАНИЦУ требования, а не измеренное значение. Подставить
        # границу как точку значило бы подтвердить код по значению, которого
        # пользователь не называл, поэтому требование проверяется как область:
        # «не менее 5» — это [5; ∞).
        lower = fact.operator in (">", ">=")
        return check_fact(units, char, Fact(
            kind="range",
            value={"min": fact.value if lower else None,
                   "max": None if lower else fact.value,
                   "min_inclusive": fact.operator != ">",
                   "max_inclusive": fact.operator != "<"},
            unit=fact.unit, polarity=fact.polarity,
            extraction_confidence=fact.extraction_confidence))
    if fact.operator == "!=":
        return "unknown", {"reason": "unsupported_operator"}

    if kind == "number":
        if not isinstance(fact.value, (int, float)) or isinstance(fact.value, bool):
            return "unknown", {"reason": "unsupported_value_shape"}
        verdict, reason = check_number(units, char, fact.value, fact)
        return verdict, ({"reason": reason} if reason else None)

    if kind == "range" and isinstance(fact.value, dict):
        verdict, reason = check_range(
            units, char, fact.value.get("min"), fact.value.get("max"), fact,
            lo_strict=not bool(fact.value.get("min_inclusive", True)),
            hi_strict=not bool(fact.value.get("max_inclusive", True)))
        return verdict, ({"reason": reason} if reason else None)

    if kind == "number_set":
        subs = [check_fact(units, char, Fact(kind="number", value=v, unit=fact.unit, operator=fact.operator,
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

    templates = [p for p in pool if p.is_template]
    blocked = [p for p in pool if p.resolve_blocked and not p.is_template]
    # Шаблон исключается ВСЕГДА, независимо от resolve_blocked: это не итоговый код
    # и он не должен попасть ни в кандидаты, ни в выбор. А заблокированная КОНЕЧНАЯ
    # позиция остаётся кандидатом-неизвестным: её несовместимость не доказана, и она
    # обязана удерживать уникальность другого кода.
    usable = [p for p in pool if not p.is_template]

    # Шаблон из ответа исключается всегда (он не итоговый код). Но конечная позиция,
    # которую нельзя проверить из-за дефекта источника, — это НЕ доказательство её
    # несовместимости: полнота области не подтверждена, и выбирать другой код нельзя.
    scope_complete = bool(catalog_scope_complete) and not blocked

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
                         scope_complete, checked_counts, units=units, pool=usable)
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
            "unresolvable_finite": [b.code for b in blocked],
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
    позицию и НИЧЕГО не подтверждают: они идут в unresolved с причиной
    uncertain_or_low_confidence_fact, а решение принимает
    пользователь или повторное извлечение.
    """
    exclusions, unresolved = [], []
    checked = supported = matched = 0
    verified_names, unverified_names = set(), {}

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
            soft = fact.fact_id in soft_ids
            if verdict == "compatible":
                if soft:
                    # Мягкий факт проверен, но это не доказательство: он не закрывает
                    # обязательное поле и не участвует в подтверждении значения.
                    unresolved.append(Unresolved(fact.fact_id, char.name,
                                                 "uncertain_or_low_confidence_fact"))
                else:
                    matched += 1
                    verified_names.add(char.name)      # значение доказано проверено
            elif verdict == "incompatible":
                if soft:
                    unresolved.append(Unresolved(fact.fact_id, char.name,
                                                 "uncertain_or_low_confidence_fact"))
                else:
                    exclusions.append(Exclusion(
                        fact_id=fact.fact_id, property=char.name, actual=describe_fact(fact),
                        rule=describe_rule(char), def_id=char.code, position=position.code))
            else:
                reason = (detail or {}).get("reason", "unknown")
                unresolved.append(Unresolved(fact.fact_id, char.name, reason))
                unverified_names.setdefault(char.name, reason)

    # обязательное свойство считается закрытым только при доказанной проверке значения:
    # наличие факта по имени (без единицы, unknown, uncertain) обязательное поле не закрывает
    missing = sorted(n for n in position.required_names() if n not in verified_names)
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
    return candidate, {
        "checked": checked, "supported": supported, "matched": matched,
        "verified": len(verified_names), "unverified": dict(unverified_names),
        "structure_hash": position.structure_hash(),
    }


def _characteristic_for_fact(position: Position, fact: Fact):
    """Характеристика позиции, к которой относится факт: по коду, иначе по имени."""
    char = position.char_by_def_id(fact.def_id) if fact.def_id else None
    if char is not None:
        return char
    same = position.chars_by_name(fact.name)
    return same[0] if len(same) == 1 else None


def _facts_conflict(facts: list, units: Units, pool=None) -> bool:
    """Противоречат ли факты друг другу.

    Сравниваем только сопоставимые утверждения: одно свойство, значение приводится к единице
    первой альтернативы позиции, поэтому «650 Вт» и «0,65 кВт» не конфликт, а разные числа —
    конфликт. Несколько допустимых значений характеристики kind=3 конфликтом не считаются.
    """
    grouped = {}
    for f in facts:
        if f.polarity != "affirmed":
            continue
        key = (f.def_id or "", normalize_name(f.name))
        grouped.setdefault(key, []).append(f)

    for (def_id, name), group in grouped.items():
        if len(group) < 2:
            continue
        position = None
        if pool:
            for p in pool:
                if _characteristic_for_fact(p, group[0]) is not None:
                    position = p
                    break
        char = _characteristic_for_fact(position, group[0]) if position is not None else None
        if char is not None and str(char.kind) == "3":
            continue                                  # множественный выбор: несколько значений законны
        values = set()
        numeric = set()
        for f in group:
            canonical = _canonical_value(units, char, f)
            values.add(canonical)
            if canonical is not None and canonical[0] == "number":
                numeric.add(round(canonical[1], 9))
        if len(values) > 1:
            return True
    return False


def _canonical_value(units: Units, char, fact: Fact):
    """Значение факта в каноническом виде для сравнения: единица приводится, если возможно."""
    if fact.kind == "number" and isinstance(fact.value, (int, float)) and not isinstance(fact.value, bool):
        value = float(fact.value)
        if char is not None:
            res = _resolve_number(units, char, value, fact)
            if res.reason is None:
                value = res.value
        return ("number", value)
    if fact.kind == "range" and isinstance(fact.value, dict):
        lo, hi = fact.value.get("min"), fact.value.get("max")
        lo_v = float(lo) if lo is not None else None
        hi_v = float(hi) if hi is not None else None
        if char is not None:
            to_first = [_canonical_scalar(units, char, x, fact) for x in (lo_v, hi_v)]
            lo_v, hi_v = to_first
        return ("range", lo_v, hi_v)
    if fact.kind == "text":
        return ("text", normalize_text(fact.value))
    if fact.kind == "text_set":
        return ("text_set", tuple(sorted(normalize_text(v) for v in (fact.value or []))))
    if fact.kind == "number_set":
        out = []
        for v in fact.value or []:
            if char is not None:
                res = _resolve_number(units, char, float(v), fact)
                out.append(round(res.value, 9) if res.reason is None else float(v))
            else:
                out.append(round(float(v), 9))
        return ("number_set", tuple(sorted(out)))
    return ("other", str(fact.value))


def _canonical_scalar(units: Units, char, value, fact: Fact):
    if value is None:
        return None
    res = _resolve_number(units, char, float(value), fact)
    return res.value if res.reason is None else float(value)


def _build_flags(candidates, survivors, evidence, soft, type_verified,
                 scope_complete, stats_by_code, units=None, pool=None) -> FlagSet:
    scope = survivors or candidates
    blocking = []
    if _facts_conflict(evidence, units, pool):
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

    # value_verified — доказанная проверка: есть поддерживаемые ограничения, есть совместимые
    # факты и НИ ОДНОГО непроверенного значения, включая пропуски обязательных полей
    unverified_reasons = {"unit_dimension_unsupported", "unsupported_value_shape", "value_units_ambiguous",
                          "user_unit_missing", "user_unit_unknown", "non_finite_value",
                          "operator_not_confirmable", "uncertain_assertion",
                          "uncertain_or_low_confidence_fact", "low_confidence_fact",
                          "reversed_user_range", "empty_user_range", "boundary_strictness_unknown"}
    has_verified = any(stats_by_code.get(c.code, {}).get("verified") for c in survivors)
    no_unverified = not any(u.reason in unverified_reasons for c in survivors for u in c.unresolved)
    value_verified = bool(survivors and has_verified and no_unverified
                          and not any(c.exclusions for c in survivors))
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
