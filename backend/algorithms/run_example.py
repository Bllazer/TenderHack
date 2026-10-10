"""Запуск одного тест-кейса из JSON: печатает результат AlgorithmC и вопрос AlgorithmD.

Использование:
    python -m algorithms.run_example algorithms/samples/example_case.json
    python -m algorithms.run_example algorithms/samples/example_case.json --json

Формат входного файла:

    {
      "title": "название кейса",
      "pool": ["28.24.11.000-00000001", "28.24.11.000-00000002"],
      "type_verified": true,
      "facts": [ { "def_id": null, "name": "Тип питания", "kind": "text",
                   "value": "От сети", "unit": null,
                   "extraction_confidence": 0.9, "polarity": "affirmed" } ]
    }

Что делает запускатель:
  1) читает пул позиций из индекса КТРУ (data/ или samples/ktru_mini_index.json);
  2) прогоняет AlgorithmC и печатает вердикт по каждой позиции;
  3) если код не уникален — вызывает AlgorithmD и печатает вопрос;
  4) если вопрос задан, отвечает значением из блока "answer" (если он есть) и повторяет C.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .AlgorithmC import Units, run_c
from .AlgorithmD import answered_from_facts, next_question
from .contract import Fact, Unit
from .ktru_index import KtruIndex

HERE = os.path.dirname(os.path.abspath(__file__))


def load_index(fixture: bool) -> KtruIndex:
    if fixture:
        path = os.path.join(HERE, "samples", "ktru_mini_index.json")
        return KtruIndex.load_json(path)
    try:
        return KtruIndex.load()
    except FileNotFoundError:
        print("полный корпус не найден, беру мини-справочник samples/ktru_mini_index.json")
        return KtruIndex.load_json(os.path.join(HERE, "samples", "ktru_mini_index.json"))


def print_result(result, index, title="AlgorithmC"):
    print("\n--- %s ---" % title)
    print("пул на входе: %d, заблокированных исключено: %d"
          % (result.counts["before"], result.counts["blocked_excluded"]))
    for c in result.candidates:
        pos = index.by_code.get(c.code)
        name = (pos.name if pos else "") or ""
        print("  %-24s %-13s %s" % (c.code, c.verdict, name[:58]))
        for e in c.exclusions:
            print("      исключено: %s = %s | %s" % (e.property, e.actual, e.rule[:70]))
        for u in c.unresolved:
            print("      не проверено: %s (%s)" % (u.property or "-", u.reason))
        if c.missing_required:
            print("      нет обязательных: %s" % ", ".join(c.missing_required[:5]))
    print("  выжившие: %s" % (result.survivors or "нет"))
    print("  флаги: %s" % json.dumps(result.flags.to_dict(), ensure_ascii=False))
    print("  selected_ktru_code: %s" % result.selected_ktru_code)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("case", help="путь к JSON с кейсом")
    ap.add_argument("--fixture", action="store_true", help="использовать мини-справочник из samples/")
    ap.add_argument("--json", action="store_true", help="вывести результат как JSON")
    args = ap.parse_args(argv)

    with open(args.case, encoding="utf-8") as fh:
        case = json.load(fh)

    index = load_index(args.fixture)
    units = Units.load()
    facts = [Fact.from_dict(f) for f in case.get("facts") or []]
    pool = index.pool_for(case.get("pool") or [])
    if not pool:
        print("пул пуст: проверьте коды позиций в поле pool")
        return 2

    result = run_c(pool, facts, index, units,
                   type_verified=bool(case.get("type_verified", True)),
                   max_candidates=0)
    if args.json:
        print(json.dumps({"title": case.get("title"), "C": result.to_dict()}, ensure_ascii=False, indent=1))
    else:
        print("кейс: %s" % case.get("title"))
        print("фактов: %d" % len(facts))
        print_result(result, index)

    question = next_question(result.survivor_positions(index), answered_from_facts(facts), index,
                             type_verified=bool(case.get("type_verified", True)),
                             blocking_issues=result.flags.blocking)
    if not args.json:
        print("\n--- AlgorithmD ---")
    if question.stop_reason:
        print("вопрос не задаётся: %s" % question.stop_reason)
    else:
        print("вопрос: «%s» (%s), ожидаемый эффект: корзин %d, худший остаток %d из %d"
              % (question.name, question.answer_kind, question.buckets, question.worst_case,
                 question.candidates_before))
        answer = case.get("answer")
        if answer:
            extra = Fact.from_dict(answer)
            print("ответ пользователя: %s = %s" % (extra.name, extra.value))
            second = run_c(result.survivor_positions(index), facts + [extra], index, units,
                           type_verified=True, max_candidates=0)
            print_result(second, index, title="AlgorithmC после ответа")
    return 0


if __name__ == "__main__":
    sys.exit(main())
