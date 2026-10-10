"""Модель A: разбор текста в факты и сопоставление с характеристиками КТРУ.

Три вещи, на которых построено решение, и все три подтверждены замерами на
реальных данных:

1. **Имена характеристик уникальны внутри позиции — 100 %** (0 повторов на
   52 255 позициях), тогда как глобально уникальны лишь 8 имён из 14 935. Значит
   `def_id` разрешается по позиции, а не по всему корпусу. Именно поэтому
   `def_id` здесь заполняется только когда это безопасно, а в остальных случаях
   остаётся пустым: AlgorithmC сам подхватит факт по имени внутри позиции.

2. **Морфология обязательна.** Сопоставление по точному совпадению теряет
   падежные формы: «мощности» не находит «Мощность». Лемматизатор (`pymorphy3`)
   в проекте уже есть и работает в BM25 — здесь он используется так же.

3. **Слабые совпадения не должны исключать коды.** Контракт объявляет порог
   `HARD_FACT_MIN_CONFIDENCE = 0.6`: ниже него AlgorithmC факт проверяет, но не
   исключает по нему позицию. Поэтому частичное совпадение получает 0.55 —
   намеренно ниже порога, чтобы догадка никогда не выбросила верный код.

Единицы не переводятся: `unit.text` — написание из текста, `unit.code` — ОКЕИ,
если написание распознано. Приведение к единице характеристики делает C.
Отдельно проверяется только размерность: «Масса» не сопоставляется с фактом,
у которого единица «мм».
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Optional

from app.catalog import Position, Property
from app.clean_supply import OKEI_SUPPORTED
from app.search import Analyzer, canon
from app.structured import StructuredFact
from app.units import get_unit

ANALYZER = Analyzer()

#: порог контракта: ниже него факт не исключает код
HARD_FACT_MIN_CONFIDENCE = 0.6


def _build_unit_codes() -> dict:
    """Написание единицы -> код ОКЕИ по ВСЕМУ справочнику, а не по 22 кодам.

    `OKEI_SUPPORTED` намеренно узкий: он объявляет, какие размерности backend
    умеет сравнивать. Но для модели A важно другое — **опознать** написание и
    отдать код. Если код не отдан, `AlgorithmC` отвечает `user_unit_unknown` и
    факт не проверяется вовсе: замерено 84 числовых факта без кода против 21
    у харнесса, и именно это мешало фактам сужать пул.

    Справочник берётся из поставки: 126 кодов и 130 написаний.
    """
    from app.clean_supply import OKEI_SUPPORTED
    table = {canon(name): code for code, name in OKEI_SUPPORTED.items()}
    candidates = [
        Path(__file__).resolve().parent.parent / "algorithms" / "data" / "okei_units.json",
        Path(__file__).resolve().parent.parent / "ktru_clean" / "okei_units.json",
        Path(__file__).resolve().parent.parent.parent / "ktru_clean" / "okei_units.json",
    ]
    try:
        from app.config import settings
        if settings.clean_supply_dir:
            candidates.insert(0, settings.clean_supply_dir / "okei_units.json")
    except Exception:
        pass
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for unit in data.get("units", []):
            code = unit.get("code")
            if not code:
                continue
            for spelling in unit.get("spellings", []):
                name = spelling.get("name")
                if name:
                    table.setdefault(canon(name), code)
        break
    return table


UNIT_CODES = _build_unit_codes()

#: Сокращения, которых нет в поставке отдельными написаниями. Каждое сверено
#: с полным названием в `okei_units.json`: Ампер 260, Ампер-час 263, Час 356,
#: Децибел 3135, Дюйм 039, Мегапаскаль 298, Квадратный миллиметр 050.
#: Внимание: в `algorithms/validation_run.py` таблица UNIT_HINTS содержит два
#: неверных кода — «мпа» -> 293 (это Гигагерц, Мегапаскаль 298) и
#: «а·ч» -> 245 (это Киловатт-час, Ампер-час 263).
UNIT_ABBREVIATIONS = {
    "мм": "003", "см": "004", "м": "006", "кг": "166", "г": "163", "л": "112",
    "мл": "111", "вт": "212", "кВт": "214", "в": "222", "а": "260", "а·ч": "263",
    "об/мин": "331", "шт": "796", "ч": "356", "мин": "355", "%": "744",
    "дб": "3135", "°с": "280", "мпа": "298", "дюйм": "039", "мм2": "050",
}
for _spelling, _code in UNIT_ABBREVIATIONS.items():
    UNIT_CODES.setdefault(canon(_spelling), _code)

#: уверенности по способу сопоставления, от сильного к слабому
CONFIDENCE = {
    "exact": 0.95,        # имя или алиас совпали точно
    "lemma": 0.90,        # совпали множества лемм с именем характеристики
    "typo": 0.90,         # уверенная опечатка: имя заменяется автоматически
    "alias_lemma": 0.75,  # совпали леммы с алиасом
    "fuzzy": 0.58,        # похоже, но неуверенно; НИЖЕ порога, не исключает
    "partial": 0.55,      # одно имя — часть другого; НИЖЕ порога, не исключает
}

#: Два порога похожести — те же, что в живой стадии A (`app/extraction.py`), чтобы
#: модель и стадия не расходились в решении «заменить или спросить».
#: Замерено на 774 однобуквенных опечатках: похожесть на истинное имя — медиана 0.900;
#: выше 0.85 попадает 80 % опечаток, и ни одной спорной (чужое имя тоже ≥ порога).
#: Максимальная похожесть на ЧУЖОЕ имя — 0.833, поэтому порог 0.85 разделяет.
TYPO_CONFIDENT = 0.85   # выше — замена имени автоматически
FUZZY_RATIO = 0.72      # ниже — ничего; между — мягкий факт «возможно, это оно»

NUMBER = re.compile(r"^[+−-]?\d+(?:[.,]\d+)?$")
NAME_VALUE = re.compile(r"^(?P<name>.*?)(?=\s*[+−-]?\d)")
#: «650вт» и «20см» — единица приклеена к числу, пробела нет.
GLUED_MEASUREMENT = re.compile(r"(?P<value>[+−-]?\d+(?:[.,]\d+)?)\s*(?P<unit>[^\d\s]+)$")


def lemma_key(text: str) -> frozenset:
    """Множество лемм — основа сопоставления, устойчивая к падежам."""
    return frozenset(ANALYZER.tokens(text or ""))


def similarity(left: str, right: str) -> float:
    """Похожесть двух написаний: 0…1 по совпадающим блокам (`SequenceMatcher`)."""
    return _similarity(canon(left or ""), canon(right or ""))


@lru_cache(maxsize=1 << 18)
def _similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, left, right).ratio()


class KnownNames(dict):
    """Словарь «леммы имени → имя» с ленивым индексом для поиска опечаток.

    Ключ индекса — первая буква и длина, а не первые две буквы: опечатка бывает во
    второй букве («Машность» ↔ «Мощность»), и по двум буквам такой кандидат не
    нашёлся бы. Та же схема в `app/extraction.py` и `app/search.py`.

    Без индекса нечёткое сравнение перебирало бы весь корпус (14 709 имён) на каждый
    фрагмент: замер на 360 карточках не укладывался в минуты.
    """

    def candidates(self, folded: str) -> list:
        index = getattr(self, "_fuzzy_index", None)
        if index is None:
            index = {}
            for name in self.values():
                key = canon(name)
                if key:
                    index.setdefault((key[0], len(key)), []).append((key, name))
            self._fuzzy_index = index
        out: list = []
        for length in range(max(1, len(folded) - 2), len(folded) + 3):
            out.extend(index.get((folded[0], length), ()))
        return out


def closest_known(text: str, known: dict) -> Optional[str]:
    """Имя из словаря, похожее на написание (опечатка), или None.

    «Мощьность» → «Мощность»: леммы не совпали, поэтому сравнивается написание.
    Порог `FUZZY_RATIO` отделяет опечатки от разных имён характеристик.
    """
    folded = canon(text or "")
    if not folded:
        return None
    if isinstance(known, KnownNames):
        candidates = known.candidates(folded)
    else:
        candidates = [(canon(name), name) for name in known.values()]
    best, best_ratio = None, FUZZY_RATIO
    for key, name in candidates:
        if abs(len(key) - len(folded)) > 2:
            continue
        ratio = _similarity(folded, key)
        if ratio >= best_ratio:
            best, best_ratio = name, ratio
    return best


def to_number(value: str) -> Optional[float]:
    try:
        return float(Decimal(value.replace(",", ".").replace("−", "-").replace(" ", "")))
    except (InvalidOperation, ValueError, AttributeError):
        return None


def is_empty_value(value: Optional[str]) -> bool:
    r"""Значение — уточнение в скобках или огрызк без содержания.

    Фрагмент «Тип стеллажа стеллаж (комплект)» даёт значением «(комплект)»: это
    не утверждение о характеристике, но как жёсткий факт оно исключает позиции.
    Замерено на реальном пуле: 48 таких исключений и 6 обнулённых карточек из 360;
    отказ от них поднял число доведённых до кода с 17 до 22 при `top=3`
    (ML_STATE.md §8.5, `C:\hack\build\value_fixes.txt`).
    """
    text = (value or "").strip()
    letters = "".join(ch for ch in text if ch.isalnum())
    return len(letters) <= 2 or (text.startswith("(") and text.endswith(")"))


# ------------------------------------------------------------------ разбор ====
@dataclass(frozen=True)
class Parsed:
    """Фрагмент, разобранный на имя, значение и единицу."""

    name: str
    value: str
    unit: Optional[str]
    kind: str            # number | text
    reason: str = ""     # почему не разобрано, если name пуст


def split_by_known_names(text: str, known: dict) -> Optional[tuple]:
    """Найти самое длинное известное имя, с которого начинается фрагмент.

    `known` — отображение «леммы имени -> исходное имя». Сравнение по леммам,
    поэтому «Мощности 650 Вт» находит «Мощность»: точное совпадение здесь
    теряло бы падежные формы, а их в карточках большинство.
    """
    words = text.split()
    for size in range(len(words), 0, -1):
        head = " ".join(words[:size])
        key = lemma_key(head)
        if key and key in known:
            rest = " ".join(words[size:]).strip(" :,=-–—")
            return known[key], rest
    # Опечатка в имени («Мощьность 650 Вт»): леммы не совпали — сравниваем написание.
    # Берётся наибольшая похожесть, а не самое длинное совпадение: лишнее слово в
    # хвосте («Мощность 650») только размывает похожесть и уводит имя.
    # Возвращается НАПИСАНИЕ, а не имя из словаря: иначе опечатка исчезла бы до
    # сопоставления, факт стал бы точным (0.95) и жёстко исключал бы коды.
    best = None
    for size in range(1, len(words) + 1):
        head = " ".join(words[:size])
        name = closest_known(head, known)
        if name is None:
            continue
        ratio = similarity(head, name)
        if best is None or ratio > best[0]:
            best = (ratio, head, " ".join(words[size:]).strip(" :,=-–—"))
    if best:
        return best[1], best[2]
    return None


def split_by_known_names_at_end(text: str, known: dict) -> Optional[tuple]:
    """Известное имя в КОНЦЕ фрагмента: «650 Вт мощность» → («Мощность», «650 Вт»).

    В карточках СТЕ порядок всегда «имя, затем значение», но в свободном описании
    значение идёт первым не реже: «650 Вт, от сети». Без этого правила такой факт
    терялся целиком. Имя ищется по леммам, поэтому «20 см длины» тоже находится.
    """
    words = text.split()
    for size in range(len(words), 0, -1):
        tail = " ".join(words[len(words) - size:])
        key = lemma_key(tail)
        if key and key in known:
            head = " ".join(words[:len(words) - size]).strip(" :,=-–—")
            if head:                     # без значения слева это не «значение → имя»
                return known[key], head
    # Опечатка в имени и порядок «значение → имя»: «650 вт мощьность». Похожесть
    # ограничивает хвост одним именем — «вт мощьность» уже не имя характеристики.
    # Как и выше, возвращается написание: мягкость факта решает сопоставление.
    best = None
    for size in range(1, len(words) + 1):
        tail = " ".join(words[len(words) - size:])
        name = closest_known(tail, known)
        if name is None:
            continue
        head = " ".join(words[:len(words) - size]).strip(" :,=-–—")
        if not head:                     # без значения слева это не «значение → имя»
            continue
        ratio = similarity(tail, name)
        if best is None or ratio > best[0]:
            best = (ratio, tail, head)
    if best:
        return best[1], best[2]
    return None


def parse_fragment(text: str, known: Optional[dict] = None) -> Parsed:
    """Разобрать фрагмент вида «Вес 2.03000 кг», «Тип застежки Молния» или «20 см длина»."""
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    if not cleaned:
        return Parsed("", "", None, "text", "empty")

    if known:
        found = split_by_known_names(cleaned, known)
        if found:
            name, rest = found
            return _finish(name, rest)
        trailing = split_by_known_names_at_end(cleaned, known)
        if trailing:
            # Значение стоит слева: имя уже каноническое, слева — значение и единица.
            name, leading = trailing
            return _finish(name, leading)

    match = NAME_VALUE.match(cleaned)
    if match and match.group("name").strip():
        return _finish(match.group("name").strip(" :,=-–—"), cleaned[match.end():])
    return Parsed("", cleaned, None, "text", "no_name")


def _finish(name: str, rest: str) -> Parsed:
    rest = rest.strip(" :,=-–—")
    if not rest:
        return Parsed(name, "", None, "text", "no_value")
    number = to_number(rest)
    if number is not None:
        return Parsed(name, rest, None, "number")
    parts = rest.rsplit(" ", 1)
    if len(parts) == 2:
        head_number = to_number(parts[0])
        if head_number is not None and get_unit(parts[1]):
            return Parsed(name, parts[0], parts[1], "number")
    glued = GLUED_MEASUREMENT.fullmatch(rest)
    if glued and get_unit(glued["unit"]):
        # «650вт» без пробела: единица приклеена к числу
        return Parsed(name, glued["value"], glued["unit"], "number")
    return Parsed(name, rest, None, "text")


# ------------------------------------------------------------- сопоставление ==
@dataclass(frozen=True)
class Match:
    property: Property
    confidence: float
    method: str

    @property
    def hard(self) -> bool:
        return self.confidence >= HARD_FACT_MIN_CONFIDENCE


def property_dimensions(prop: Property) -> set:
    """Размерности, встречающиеся среди допустимых значений характеристики."""
    dims = set()
    for allowed in prop.allowed:
        unit = get_unit(allowed.unit or allowed.raw_unit)
        if unit:
            dims.add(unit.dimension)
    return dims


def dimension_compatible(prop: Property, unit: Optional[str]) -> bool:
    """Единица факта не должна противоречить характеристике.

    Если у характеристики единиц нет, ограничение не применяется: отсутствие
    данных не является доказательством несовместимости. Если единица факта
    неизвестна справочнику — тоже пропускаем, решение остаётся за C.
    """
    if not unit:
        return True
    fact_unit = get_unit(unit)
    if fact_unit is None:
        return True
    dims = property_dimensions(prop)
    if not dims:
        return True
    return fact_unit.dimension in dims


def match_in_position(position: Position, name: str, unit: Optional[str] = None) -> list:
    """Сопоставить имя факта с характеристиками ОДНОЙ позиции.

    Возвращает список Match, от сильного к слабому. Имена внутри позиции
    уникальны, поэтому при точном или лемматизированном совпадении список
    состоит из одного элемента — это и есть разрешение `def_id`.
    """
    if not name:
        return []
    exact_key = canon(name)
    lemmas = lemma_key(name)
    if not lemmas:
        return []

    out: list[Match] = []
    partials: list[Property] = []
    near: list[tuple[float, Property]] = []
    for prop in position.properties:
        if not dimension_compatible(prop, unit):
            continue
        variants = [prop.name, *prop.aliases]
        if any(canon(variant) == exact_key for variant in variants):
            out.append(Match(prop, CONFIDENCE["exact"], "exact"))
            continue
        if lemmas == lemma_key(prop.name):
            out.append(Match(prop, CONFIDENCE["lemma"], "lemma"))
            continue
        if any(lemmas == lemma_key(alias) for alias in prop.aliases):
            out.append(Match(prop, CONFIDENCE["alias_lemma"], "alias_lemma"))
            continue
        prop_lemmas = lemma_key(prop.name)
        if prop_lemmas and (lemmas < prop_lemmas or prop_lemmas < lemmas):
            # Неполное имя: «минимальный диаметр» ⊂ «Минимальный диаметр зажима патрона».
            # Кандидаты собираются отдельно: привязка годится, только если такой
            # кандидат РОВНО ОДИН. «диаметр» подходит и минимуму, и максимуму — тогда
            # привязки нет, значение уходит на уточнение (как в живой стадии A).
            partials.append(prop)
            continue
        best = max((similarity(name, variant) for variant in variants), default=0.0)
        if best >= FUZZY_RATIO:
            near.append((best, prop))
    if out:
        return sorted(out, key=lambda m: -m.confidence)
    if len(partials) == 1:
        return [Match(partials[0], CONFIDENCE["partial"], "partial")]
    # Опечатки считаются отдельно от точных совпадений, потому что решение зависит
    # от единственности и длины: если похожи ДВА имени, автозамена запрещена, а
    # короткие слова (меньше 5 букв) слишком легко похожи друг на друга — «маса»
    # остаётся мягким фактом, как и в живой стадии A.
    confident = [prop for ratio, prop in near
                 if ratio >= TYPO_CONFIDENT and len(exact_key) >= 5]
    if len(confident) == 1:
        return [Match(confident[0], CONFIDENCE["typo"], "typo")]
    return sorted((Match(prop, CONFIDENCE["fuzzy"], "fuzzy") for _, prop in near),
                  key=lambda m: -m.confidence)


# --------------------------------------------------------------- разрешение ==
@dataclass
class Resolution:
    """Что удалось выяснить про факт относительно пула позиций."""

    def_id: Optional[str] = None
    canonical_name: Optional[str] = None   # имя характеристики из КТРУ, не из текста
    method: str = "unmatched"
    confidence: float = 0.0
    alternatives: list = field(default_factory=list)
    positions_matched: int = 0          # в скольких позициях пула нашлось имя
    positions_resolvable: int = 0       # в скольких из них совпадение однозначно
    ambiguous_in: list = field(default_factory=list)

    @property
    def resolvable_per_position(self) -> bool:
        """Сможет ли C разрешить факт по имени внутри каждой позиции пула."""
        return self.positions_matched > 0 and self.positions_resolved_ok()

    def positions_resolved_ok(self) -> bool:
        return not self.ambiguous_in


def resolve(parsed: Parsed, positions: list, unit: Optional[str] = None) -> Resolution:
    """Разрешить факт относительно пула позиций.

    `def_id` заполняется только когда он однозначен для всего пула. Иначе он
    остаётся пустым: подставлять код одной позиции в факт, общий для пула,
    значило бы приписать ему чужую семантику.
    """
    unit = unit if unit is not None else parsed.unit
    result = Resolution()
    if not parsed.name:
        result.method = parsed.reason or "no_name"
        return result

    hard_matches: list[Match] = []
    for position in positions:
        matches = match_in_position(position, parsed.name, unit)
        if not matches:
            continue
        result.positions_matched += 1
        strong = [m for m in matches if m.method != "partial"]
        if len(strong) > 1:
            result.ambiguous_in.append(position.code)
        elif strong:
            result.positions_resolvable += 1
        hard_matches.extend(strong or matches[:1])

    if not hard_matches:
        result.method = "unmatched"
        return result

    best = max(hard_matches, key=lambda m: m.confidence)
    result.method = best.method
    result.confidence = best.confidence
    # Каноническое имя подставляем только для уверенных совпадений. Для слабого
    # (partial) имени «Цвет» -> «Цвет мела» подстановка означала бы, что факт про
    # цвет рюкзака заявляет характеристику «цвет мела»: замерено 28 карточек, где
    # такой подстановкой пул обнулялся целиком.
    strong = [m for m in hard_matches if m.method != "partial"]
    if strong:
        result.canonical_name = Counter(m.property.name for m in strong).most_common(1)[0][0]
    else:
        result.canonical_name = parsed.name

    codes = {m.property.id for m in hard_matches}
    if len(codes) == 1:
        # Все совпавшие позиции указывают на ОДИН код характеристики. Коды
        # переиспользуются между позициями одной семьи, поэтому совпадение
        # в нескольких позициях вовсе не означает неоднозначность: 12 058 имён
        # из 14 928 сопоставляются ровно с одним кодом.
        result.def_id = next(iter(codes))
    else:
        # В разных позициях пула совпали разные коды. Основным ставим самый
        # частый: AlgorithmC сначала пробует char_by_def_id, и если он есть в
        # позиции — факт проверяется. Для позиций, где этого кода нет, C и так
        # падает на поиск по имени, так что основной код разрешимость только
        # добавляет. Остальные уходят в definition_alternatives как происхождение —
        # их читает алгоритм D, чтобы не спрашивать одно и то же дважды.
        counts = Counter(m.property.id for m in hard_matches)
        names = {m.property.id: m.property.name for m in hard_matches}
        ordered = [code for code, _ in counts.most_common()]
        result.def_id = ordered[0]
        result.alternatives = [
            {"def_id": code, "name": names[code]} for code in ordered[1:4]
        ]
    return result


def known_names_from_variants(variants) -> dict:
    """Словарь разбора: «леммы имени -> имя» из произвольного набора написаний.

    Разбор и разрешение — разные задачи, и словари у них разные. Разбирать надо
    по **всему корпусу**: имя «Материал» существует в КТРУ, даже если в пуле
    конкретной карточки такой характеристики нет. Если строить словарь разбора
    по пулу, фрагменты вида «Материал Полиэстер» теряются целиком — замерено
    965 потерянных фрагментов на 360 карточках.

    Разрешение, наоборот, обязано быть по пулу: там имя должно найтись среди
    характеристик конкретной позиции.
    """
    known: dict = KnownNames()
    for variant in variants:
        key = lemma_key(variant)
        if key:
            known.setdefault(key, variant)
    return known


def known_names(positions: list) -> dict:
    """Словарь разбора только по пулу — для случаев, когда корпуса под рукой нет."""
    return known_names_from_variants(
        variant
        for position in positions
        for prop in position.properties
        for variant in (prop.name, *prop.aliases)
    )


#: Измерение в любом месте фрагмента: «дрель 650 вт», «650вт», «0,65 кВт».
MEASUREMENT_ANYWHERE = re.compile(r"(?P<value>[+−-]?\d+(?:[.,]\d+)?)\s*(?P<unit>[A-Za-zА-Яа-яЁё/%²³·]+)")


def unique_property_name(positions: list, dimension: str) -> Optional[str]:
    """Единственная характеристика позиций с допустимыми значениями этой размерности."""
    names = []
    for position in positions:
        for prop in position.properties:
            if prop.value_type != "number":
                continue
            for item in prop.allowed:
                allowed_unit = get_unit(item.unit or item.raw_unit)
                if allowed_unit is not None and allowed_unit.dimension == dimension:
                    names.append(prop.name)
                    break
    unique = sorted(set(names))
    return unique[0] if len(unique) == 1 else None


def bind_measurements(fragment: str, positions: list) -> list:
    """Измерения без имени характеристики: её называет размерность единицы.

    Реальный запрос «дрель 650 вт»: имени характеристики в тексте нет, но «Вт» — это
    мощность, и у дрели такая характеристика есть. Привязываем, только если в позиции
    она **одна**: «минимальный» и «максимальный диаметр» оба в миллиметрах, и угадывать
    между ними нельзя — значение остаётся неразобранным.

    Измерений во фрагменте может быть несколько («дрель 650 вт 220 в»): каждое
    привязывается по своей единице.
    """
    bound = []
    for match in MEASUREMENT_ANYWHERE.finditer(fragment):
        unit = get_unit(match["unit"])
        if unit is None:
            continue
        name = unique_property_name(positions, unit.dimension)
        if name is None:
            continue
        bound.append(Parsed(name, match["value"], match["unit"], "number"))
    return bound


def facts_from_fragments(fragments: list, positions: list, known: Optional[dict] = None) -> tuple:
    """Фрагменты карточки СТЕ -> факты формата контракта + статистика.

    `known` — словарь разбора; если не передан, строится по пулу. Для реального
    корпуса его надо передавать собранным по всем характеристикам КТРУ: иначе
    теряются имена, которых нет в конкретной позиции (см. `known_names_from_variants`).
    """
    known = known if known is not None else known_names(positions)
    facts, stats = [], {"parsed": 0, "unparsed": 0,
                        "with_def_id": 0, "per_position": 0,
                        "ambiguous": 0, "unmatched": 0, "bound_by_unit": 0,
                        "dropped_empty_value": 0}

    def count(resolution) -> None:
        if resolution.ambiguous_in:
            stats["ambiguous"] += 1
        elif resolution.method == "unmatched":
            stats["unmatched"] += 1
        elif resolution.def_id:
            # код характеристики известен однозначно для всего пула
            stats["with_def_id"] += 1
        else:
            # имя найдено, но код разный в разных позициях пула: def_id пуст,
            # и C разрешает факт по имени внутри каждой позиции
            stats["per_position"] += 1

    def emit_readings(readings, index) -> None:
        """Измерения, привязанные по размерности: своё имя, свой код, свой id."""
        stats["bound_by_unit"] += len(readings)
        for order, reading in enumerate(readings):
            resolution = resolve(reading, positions)
            count(resolution)
            facts.append(_to_fact(reading, resolution, f"ste:{index}:{order}", fragments[index]))

    for index, fragment in enumerate(fragments):
        parsed = parse_fragment(fragment, known)
        if not parsed.name:
            readings = bind_measurements(fragment, positions)
            if not readings:
                stats["unparsed"] += 1
                continue
            stats["parsed"] += 1
            emit_readings(readings, index)
            continue
        if parsed.kind == "text" and is_empty_value(parsed.value):
            # Факт без утверждения: имя разобрано, а значение пустое («(комплект)»).
            # Выпускать его нельзя — он исключает позиции, ничего не сообщая.
            stats["dropped_empty_value"] += 1
            continue
        stats["parsed"] += 1
        resolution = resolve(parsed, positions)
        if resolution.method == "unmatched":
            # Имя есть, но такой характеристики у позиции нет. Если в тексте остались
            # измерения, размерность которых называет единственную характеристику
            # («дрель 650 вт»), значения принадлежат ей, а не названию товара.
            readings = bind_measurements(fragment, positions)
            if readings:
                emit_readings(readings, index)
                continue
        count(resolution)
        facts.append(_to_fact(parsed, resolution, f"ste:{index}", fragment))
    return facts, stats


def _to_fact(parsed: Parsed, resolution: Resolution, identifier: str, raw: str) -> StructuredFact:
    unit = None
    if parsed.unit:
        from app.structured import UnitRef
        # Каноническое имя из справочника единиц — чтобы C сразу видел размерность,
        # а код ОКЕИ берём из полного справочника, а не из узкого OKEI_SUPPORTED.
        resolved = get_unit(parsed.unit)
        unit = UnitRef(code=UNIT_CODES.get(canon(parsed.unit if resolved is None else resolved.name))
                       or UNIT_CODES.get(canon(parsed.unit)),
                       text=parsed.unit)
    value = parsed.value
    if parsed.kind == "number":
        number = to_number(parsed.value)
        if number is not None:
            value = number
    return StructuredFact(
        kind="number" if parsed.kind == "number" else "text",
        value=value,
        name=resolution.canonical_name or parsed.name,
        def_id=resolution.def_id,
        unit=unit,
        operator=None,
        polarity="affirmed",
        negation=None,
        span=None,
        extraction_method=f"fragment+{resolution.method}",
        extraction_confidence=resolution.confidence,
        definition_alternatives=resolution.alternatives,
        fact_id=identifier,
    )
