"""Валидация: полный прогон стадия A -> C -> D на образце вывода модели и на карточках СТЕ.

Что делает:
  1) берёт образец вывода модели (samples/stage_model_output.json) — факты с готовым def_id;
  2) берёт карточки СТЕ из XLSX (samples/ste_*.json) и сопоставляет их свойства
     со справочником КТРУ — это заглушка стадии «Сопоставление свойств»;
  3) для каждого товара раскрывает пул кандидатов и прогоняет AlgorithmC;
  4) если код не уникален — вызывает AlgorithmD и, если вопрос задан, отвечает
     значением реального кандидата из пула и повторяет C;
  5) печатает по каждому товару результат и агрегаты, сохраняет отчёт в validation_report.json.

Запуск:
    python -m algorithms2.validation_run                 # мини-фикстура + карточки СТЕ
    python -m algorithms2.validation_run --full          # полный корпус КТРУ вместо фикстуры
    python -m algorithms2.validation_run --cards 200     # сколько карточек СТЕ брать
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import time
from typing import Optional

from .AlgorithmC import Units, run_c
from .AlgorithmD import answered_from_facts, next_question
from .contract import Fact, Unit
from .ktru_index import KtruIndex, normalize_name

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(HERE, "samples")

#: свойство товара -> значение; «Вес 2.03000 кг», «Мощность 650 Вт», «Тип патрона ключевой»
FRAGMENT_RE = re.compile(
    r"^(?P<name>[^\d]{3,90}?)\s*[:\-]?\s*(?P<value>-?\d+(?:[.,]\d+)?)\s*(?P<unit>[^\s\d]{0,12})?\s*$")
TEXT_FRAGMENT_RE = re.compile(r"^(?P<name>[A-Za-zА-Яа-яЁё][^|]{2,90}?)\s+(?P<value>[A-Za-zА-Яа-яЁё][^|]{1,60})$")
NUMBERISH_RE = re.compile(r"^[+-]?\d+(?:[.,]\d+)?$")

#: сокращения, которые модель/правила приводят к ОКЕИ (полный словарь — в data/example_unit_strings.json)
UNIT_HINTS = {
    "мм": ("003", "Миллиметр"), "см": ("004", "Сантиметр"), "м": ("006", "Метр"),
    "кг": ("166", "Килограмм"), "г": ("163", "Грамм"), "л": ("112", "Литр"),
    "мл": ("111", "Миллилитр"), "вт": ("212", "Ватт"), "кВт": ("214", "Киловатт"),
    "квт": ("214", "Киловатт"), "в": ("222", "Вольт"), "а": ("260", "Ампер"),
    "а·ч": ("245", "Ампер-час"), "об/мин": ("331", "Оборот в минуту"), "шт": ("796", "Штука"),
    "ч": ("356", "Час"), "мин": ("355", "Минута"), "%": ("744", "Процент"),
    "дб": ("3135", "Децибел"), "°с": ("280", "Градус Цельсия"), "мпа": ("293", "Мегапаскаль"),
    "па": ("294", "Паскаль"), "дюйм": ("039", "Дюйм"), "мм2": ("050", "Квадратный миллиметр"),
}


# --------------------------------------------------- стадия «сопоставление свойств»
def tokens_of(text: str) -> set:
    """Слова длиной от 4 символов по границам слов (не обрезая начала, как {4,})."""
    return {m.group(0).lower() for m in re.finditer(r"\w+", text or "") if len(m.group(0)) >= 4}


class PropertyMatcher:
    """Сопоставляет имя свойства из текста с определениями справочника КТРУ.

    Заглушка стадии «Сопоставление свойств в категориях»: разбор фрагмента идёт по самому
    длинному известному имени свойства (как требует постановка), затем имя ищется в словаре:
    точное совпадение -> нормализованное -> вхождение.
    """

    def __init__(self, index: KtruIndex):
        self.index = index
        self.by_exact = collections.defaultdict(set)
        self.by_normalized = collections.defaultdict(set)
        self.display_names = {}
        for p in index.by_code.values():
            if p.is_template:
                continue
            for c in p.characteristics:
                self.by_exact[c.name].add(c.code)
                self.by_normalized[normalize_name(c.name)].add(c.code)
                self.display_names.setdefault(normalize_name(c.name), c.name)
        # имена для разбора по словарю: длинные раньше коротких
        self.name_prefixes = sorted(
            {normalize_name(name) for name in self.by_exact if normalize_name(name)},
            key=len, reverse=True)
        # токены характеристик по семействам — считаем один раз, иначе поиск пула
        # перебирает 234 тыс. характеристик на каждую карточку
        self.family_tokens = {}
        for family, positions in index.families.items():
            tokens = set()
            for p in positions:
                for c in p.characteristics:
                    tokens.update(tokens_of(normalize_name(c.name)))
            if tokens:
                self.family_tokens[family] = tokens
        self.stats = collections.Counter()

    def families_for_tokens(self, tokens: set, max_families: int = 3, relative: float = 0.5) -> list:
        """Семейства, чьи характеристики покрывают слова описания товара.

        Порог относительный: берём семейства с оценкой не ниже `relative` от лучшей.
        Абсолютный порог на коротких названиях отсекал всё.
        """
        scored = []
        for family, family_tokens in self.family_tokens.items():
            hits = len(tokens & family_tokens)
            if hits:
                scored.append((hits, family))
        if not scored:
            return []
        scored.sort(reverse=True)
        best = scored[0][0]
        out = []
        for hits, family in scored:
            if hits < max(1, int(best * relative)):
                break
            out.append(family)
            if len(out) >= max_families:
                break
        return out

    # ------------------------------------------------------------ разбор фрагмента
    def split_fragment(self, fragment: str):
        """Делит «Тип застежки Молния» на (имя свойства, значение).

        Возвращает (name, value_text, is_number, unit_text) либо None, если имени свойства
        в словаре нет — такой фрагмент не участвует в сужении и попадает в неразобранные.
        """
        text = re.sub(r"\s+", " ", fragment or "").strip()
        if not text:
            return None
        normalized = normalize_name(text)
        for name in self.name_prefixes:
            if not normalized.startswith(name):
                continue
            if len(normalized) > len(name) and normalized[len(name)] not in " :,=-–—":
                # имя должно быть отдельным словом: «Тип» не должен съесть «Типичный»
                continue
            rest = text[len(name):].lstrip(" :,=-–—")
            if not rest:
                continue
            parsed = parse_value(rest)
            if parsed:
                return (name, parsed[0], parsed[1], parsed[2], name)
        return None

    def resolve(self, name: str) -> tuple[Optional[str], str, float]:
        """(def_id, способ, уверенность). Неоднозначное имя -> без def_id, искать внутри позиции."""
        if not name:
            return None, "empty", 0.0
        key = normalize_name(name)
        if key in self.by_normalized and len(self.by_normalized[key]) == 1:
            self.stats["exact"] += 1
            return next(iter(self.by_normalized[key])), "exact", 0.9
        codes = self.by_normalized.get(key)
        if codes and len(codes) == 1:
            self.stats["normalized"] += 1
            return next(iter(codes)), "normalized", 0.8
        if codes:
            self.stats["ambiguous_name"] += 1
            return None, "ambiguous_name", 0.6
        self.stats["not_mapped"] += 1
        return None, "not_mapped", 0.0

    def display_name(self, normalized: str) -> str:
        if normalized in self.display_names:
            return self.display_names[normalized]
        return normalized


VALUE_WITH_UNIT_RE = re.compile(r"^(?P<num>-?\d+(?:[.,]\d+)?)\s*(?P<unit>[^\d\s]{0,10})?$")


def parse_value(rest: str):
    """«2.03000 кг» -> (текст, True, единица); «Молния» -> (текст, False, '')."""
    text = re.sub(r"\s+", " ", rest or "").strip(" .,;")
    if not text:
        return None
    m = VALUE_WITH_UNIT_RE.match(text)
    if m:
        unit = (m.group("unit") or "").strip(" .,;")
        return ("%s|%s" % (m.group("num"), unit), True, unit)
    if len(text) > 120:
        text = text[:120]
    return (text, False, "")


def facts_from_fragments(fragments: list, matcher: PropertyMatcher) -> tuple[list, dict]:
    """Сырые фрагменты карточки -> список фактов. Второй элемент — статистика разбора."""
    facts, info = [], collections.Counter()
    for index, fragment in enumerate(fragments):
        split = matcher.split_fragment(fragment)
        if not split:
            info["unparsed"] += 1
            continue
        normalized_name, value_text, is_number, unit_text, _ = split
        name = matcher.display_name(normalized_name)
        def_id, method, confidence = matcher.resolve(name)
        info["mapping_" + method] += 1
        if confidence < 0.5:
            info["facts_dropped_unmapped"] += 1
            continue
        unit = None
        if is_number and unit_text:
            code, unit_name = UNIT_HINTS.get(unit_text.lower().replace(" ", ""), (None, unit_text))
            unit = Unit(code=code, text=unit_name)
        fact_id = "ste-%d" % index
        if is_number:
            raw_number = value_text.split("|")[0].replace(",", ".")
            fact = Fact(kind="number", value=float(raw_number), name=name, def_id=def_id,
                        unit=unit, extraction_method="dictionary_name",
                        extraction_confidence=confidence, fact_id=fact_id)
        else:
            fact = Fact(kind="text", value=value_text, name=name, def_id=def_id,
                        extraction_method="free_text", extraction_confidence=confidence,
                        fact_id=fact_id)
        facts.append(fact)
    return facts, dict(info)


# ------------------------------------------------------------------ пул кандидатов
def pool_by_title(index: KtruIndex, matcher: "PropertyMatcher", title: str) -> list:
    """Черновой поиск пула кандидатов по названию товара — заглушка вместо лексического поиска B.

    Слова названия ищутся среди имён характеристик семейства: пересечение токенов.
    Это грубая замена BM25+эмбеддингов, но она не пускает в пул посторонние семейства.
    """
    tokens = tokens_of(title)
    if not tokens:
        return []
    pool = []
    for family in matcher.families_for_tokens(tokens):
        pool.extend(index.family_pool(family))
    return pool


def answer_for(question, pool: list) -> Optional[Fact]:
    """Ответ пользователя: берём допустимое значение первого кандидата пула."""
    char = None
    for p in pool:
        char = p.char_by_def_id(question.def_id) or p.char_by_name(question.name)
        if char is not None:
            break
    if char is None or not char.alternatives:
        return None
    alt = char.alternatives[0]
    unit = Unit(code=alt.unit_code, text=alt.unit_text) if (alt.unit_code or alt.unit_text) else None
    if alt.kind == "text":
        return Fact(kind="text", value=alt.text, name=char.name, def_id=char.code, unit=None,
                    extraction_method="user_input", extraction_confidence=1.0)
    if alt.kind == "number":
        return Fact(kind="number", value=alt.number, name=char.name, def_id=char.code, unit=unit,
                    extraction_method="user_input", extraction_confidence=1.0)
    mid = None
    if alt.lo is not None and alt.hi is not None:
        mid = (alt.lo + alt.hi) / 2.0
    elif alt.lo is not None:
        mid = alt.lo + 1
    elif alt.hi is not None:
        mid = alt.hi - 1
    return Fact(kind="number", value=mid, name=char.name, def_id=char.code, unit=unit,
                extraction_method="user_input", extraction_confidence=1.0)


# ----------------------------------------------------------------------- прогон
def run_case(title, pool, facts, index, units, max_questions=5) -> dict:
    """C -> D -> ответ -> снова C. Возвращает компактную запись результата."""
    stages = []
    result = run_c(pool, facts, index, units, type_verified=True)
    stages.append(_stage_record(result, len(pool), asked=None))
    false_exclusion = None

    # ни одного кандидата — проверяем, не виноваты ли мягкие факты (низкая уверенность)
    if result.flags.blocking and not result.survivors:
        soft = [f for f in facts if not f.is_hard]
        if soft:
            strict = [f for f in facts if f.is_hard]
            retry = run_c(pool, strict, index, units, type_verified=True)
            if retry.survivors:
                false_exclusion = {
                    "soft_facts": len(soft),
                    "without_soft": len(retry.survivors),
                    "note": "исключение вызвано фактами с уверенностью ниже порога",
                }
                result = retry
                stages.append(_stage_record(result, len(pool), asked="без мягких фактов"))

    asked, current_pool, current_facts = set(), result.survivor_positions(index), list(facts)
    dialogue = []
    for _ in range(max_questions):
        if len(current_pool) <= 1:
            break
        question = next_question(current_pool, answered_from_facts(current_facts) | asked, index,
                                 type_verified=True, blocking_issues=result.flags.blocking)
        if question.stop_reason:
            dialogue.append({"stop": question.stop_reason, "candidates": len(current_pool)})
            break
        answer = answer_for(question, current_pool)
        dialogue.append({
            "question": question.name, "def_id": question.def_id, "answer_kind": question.answer_kind,
            "buckets": question.buckets, "worst_case": question.worst_case,
            "candidates_before": len(current_pool),
        })
        if answer is None:
            break
        if question.def_id:
            asked.add(question.def_id)
        asked.add(question.name)
        asked.add(normalize_name(question.name))
        current_facts.append(answer)
        result = run_c(current_pool, current_facts, index, units, type_verified=True,
                       max_candidates=0)
        current_pool = result.survivor_positions(index)
        stages.append(_stage_record(result, len(current_pool), asked=question.name))

    return {
        "title": title,
        "pool_size": len(pool),
        "facts": len(facts),
        "stages": stages,
        "questions": dialogue,
        "false_exclusion": false_exclusion,
        "final_candidates": [c.code for c in result.candidates if c.verdict != "incompatible"][:10],
        "final_count": len(current_pool),
        "selected": result.selected_ktru_code,
        "flags": result.flags.to_dict(),
    }


def _stage_record(result, survivors, asked):
    return {
        "asked": asked,
        "counts": result.counts,
        "survivors": survivors,
        "ktru_unique": result.flags.ktru_unique,
        "value_verified": result.flags.value_verified,
        "fields_complete": result.flags.fields_complete,
        "indistinguishable": result.flags.value_indistinguishable,
        "blocking": result.flags.blocking,
        "selected": result.selected_ktru_code,
    }


# ------------------------------------------------------------------------ отчёт
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", action="store_true",
                    help="мини-справочник из samples/ вместо полного корпуса (быстро, мало семейств)")
    ap.add_argument("--cards", type=int, default=120, help="сколько карточек СТЕ обработать на файл")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    t0 = time.time()
    if args.fixture:
        index = KtruIndex.load_json(os.path.join(SAMPLES, "ktru_mini_index.json"))
    else:
        index = KtruIndex.load()
    units = Units.load()
    print("справочник: %s" % index.stats())

    matcher = PropertyMatcher(index)
    report = {"catalog": index.version, "catalog_stats": index.stats(), "cases": [], "cards": {}}

    # ---------------------------------------------------- 1. образец вывода модели
    stage_path = os.path.join(SAMPLES, "stage_model_output.json")
    if os.path.exists(stage_path):
        with open(stage_path, encoding="utf-8") as fh:
            sample = json.load(fh)
        print("\n=== стадия A: образец вывода модели (%d товаров) ===" % len(sample["items"]))
        for item in sample["items"]:
            pool = index.pool_for(item["pool"])
            facts = [Fact.from_dict(f) for f in item["facts"]]
            case = run_case(item["title"], pool, facts, index, units)
            report["cases"].append(case)
            _print_case(case, args.quiet)

    # ------------------------------------------------------------- 2. карточки СТЕ
    for name in sorted(os.listdir(SAMPLES)):
        if not name.startswith("ste_") or not name.endswith(".json"):
            continue
        with open(os.path.join(SAMPLES, name), encoding="utf-8") as fh:
            cards = json.load(fh)["cards"][:args.cards]
        print("\n=== карточки СТЕ: %s (%d) ===" % (name, len(cards)))
        agg = collections.Counter()
        examples = []
        for card in cards:
            pool = pool_by_title(index, matcher, card["title"])
            if not pool:
                agg["no_pool"] += 1
                continue
            facts, info = facts_from_fragments(card["fragments"], matcher)
            agg.update(info)
            mapped = sum(1 for f in facts if f.def_id)
            agg["cards"] += 1
            agg["facts"] += len(facts)
            agg["facts_mapped"] += mapped
            case = run_case(card["title"], pool, facts, index, units)
            if len(examples) < 2:
                examples.append((card, case))
            if case["false_exclusion"]:
                agg["false_exclusion"] += 1
            if case["selected"]:
                agg["resolved"] += 1
            elif case["final_count"] == 1:
                agg["one_left_unresolved"] += 1
            elif case["final_count"] == 0:
                agg["no_candidates"] += 1
            elif case["questions"] and case["questions"][-1].get("stop") == "value_indistinguishable":
                agg["indistinguishable"] += 1
            else:
                agg["still_ambiguous"] += 1
        report["cards"][name] = dict(agg)
        _print_card_aggregate(agg)
        for card, case in examples:
            print("   пример: %s" % (card["title"] or "")[:70])
            print("      пул=%d фактов=%d -> осталось=%d, вопросов=%d, выбран=%s"
                  % (case["pool_size"], case["facts"], case["final_count"],
                     len(case["questions"]), case["selected"]))
            if case["questions"]:
                print("      первый вопрос: %s" % json.dumps(case["questions"][0], ensure_ascii=False)[:160])

    print("\nсопоставление свойств: %s" % dict(matcher.stats))
    print("время: %.1f с" % (time.time() - t0))
    out = os.path.join(HERE, "validation_report.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    print("отчёт: %s" % os.path.basename(out))
    return 0


def _print_case(case, quiet):
    if quiet:
        return
    print("  %s" % case["title"])
    print("     пул=%d, фактов=%d" % (case["pool_size"], case["facts"]))
    for i, stage in enumerate(case["stages"], 1):
        mark = "  вопрос: %s" % stage["asked"] if stage["asked"] else "  вход"
        print("     шаг %d%s: пул %d -> осталось %d (совместимо %d, исключено %d)"
              % (i, mark, stage["counts"]["before"], stage["survivors"],
                 stage["counts"]["compatible_after"], stage["counts"]["incompatible"]))
    for step in case["questions"]:
        if "stop" in step:
            print("     D остановился: %s (кандидатов %d)" % (step["stop"], step["candidates"]))
        else:
            print("     D спрашивает «%s» (%s): было %d, корзин %d, худший остаток %d"
                  % (step["question"], step["answer_kind"], step["candidates_before"],
                     step["buckets"], step["worst_case"]))
    print("     итог: осталось %d, выбран=%s, блокировки=%s"
          % (case["final_count"], case["selected"], case["flags"]["blocking_issues"] or "нет"))


def _print_card_aggregate(agg):
    cards = agg.get("cards", 0)
    if not cards:
        print("  ни одной карточки не обработано (пул не найден: %d)" % agg.get("no_pool", 0))
        return
    print("  карточек: %d | фактов: %d, из них сопоставлено: %d (%.1f%%)"
          % (cards, agg["facts"], agg["facts_mapped"], 100.0 * agg["facts_mapped"] / max(agg["facts"], 1)))
    print("  выбрано уникально: %d | один кандидат без resolved: %d | неразличимые: %d | без кандидатов: %d | остались неоднозначными: %d"
          % (agg.get("resolved", 0), agg.get("one_left_unresolved", 0), agg.get("indistinguishable", 0),
             agg.get("no_candidates", 0), agg.get("still_ambiguous", 0)))
    print("  ложных исключений (спасено отказом от мягких фактов): %d" % agg.get("false_exclusion", 0))
    print("  способ сопоставления: exact=%d, normalized=%d, partial=%d, ambiguous=%d, не найдено=%d, не разобрано=%d"
          % (agg.get("mapping_exact", 0), agg.get("mapping_normalized", 0), agg.get("mapping_partial", 0),
             agg.get("mapping_ambiguous_name", 0), agg.get("mapping_not_mapped", 0), agg.get("unparsed", 0)))


if __name__ == "__main__":
    raise SystemExit(main())
