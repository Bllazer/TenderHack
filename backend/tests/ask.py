"""Ручная проверка поиска: задать запрос и увидеть, что находит модель.

Запуск (из каталога backend):
    python tests/ask.py "шуруповерт" "шкаф для документов деревянный"
    python tests/ask.py                 # режим диалога, каталог грузится один раз

Показывает по каждому запросу: время, топ-5 семейств с названием и вкладом каналов
(BM25 / E5 / CHAR), а для первого кандидата — раскрытый список конечных кодов.

Загрузка каталога и модели занимает около 17 секунд; прогресс пишется в stderr,
поэтому stdout остаётся чистым для перенаправления в файл.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.catalog import load_snapshot  # noqa: E402
from app.search import E5Encoder, Search  # noqa: E402

CATALOG_DIR = Path(os.environ.get("ASK_CATALOG_DIR", "resources/catalog-build"))
MODEL_DIR = Path(os.environ.get("ASK_MODEL_DIR", "resources/model"))


def progress(message: str) -> None:
    """Прогресс в stderr, чтобы не засорять stdout."""
    print(f"  … {message}", file=sys.stderr, flush=True)


def report(search: Search, data, query: str, top: int, show_pool: int) -> None:
    started = time.time()
    candidates = search.find(query)
    elapsed = (time.time() - started) * 1000
    print("=" * 84)
    print(f"ЗАПРОС: {query!r}    ({elapsed:.0f} мс)")
    print("=" * 84)
    for rank, candidate in enumerate(candidates[:top], 1):
        category = next(c for c in data.catalog.categories if c.id == candidate.id)
        positions, complete = data.scopes[candidate.id]
        flags = []
        if not complete:
            flags.append("область неполная")
        if category.link_verified:
            flags.append("тип проверен структурно")
        suffix = ("  [" + ", ".join(flags) + "]") if flags else ""
        print(f"  {rank}. {category.id}")
        print(f"     {category.name}{suffix}")
        print(f"     вклад каналов: {', '.join(candidate.reasons)}   "
              f"RRF {candidate.rank_score:.4f}   конечных кодов в области: {len(positions)}")

    if not candidates:
        print()
        return

    positions, complete = data.scopes[candidates[0].id]
    print(f"\n  Раскрытие первого кандидата ({len(positions)} конечных кодов"
          + ("" if complete else ", область неполная") + "):")
    for position in positions[:show_pool]:
        required = sum(1 for prop in position.properties if prop.required)
        unsupported = sum(1 for prop in position.properties
                          if prop.value_type == "unsupported")
        note = []
        if position.resolve_blocked:
            note.append("заблокирована: " + ",".join(position.resolve_blocked_reason or ()))
        if unsupported:
            note.append(f"неподдержанных свойств: {unsupported}")
        print(f"     {position.code}  {position.name[:52]:52} "
              f"свойств {len(position.properties)} (обязательных {required})"
              + ("  [" + "; ".join(note) + "]" if note else ""))
    if len(positions) > show_pool:
        print(f"     … ещё {len(positions) - show_pool}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("queries", nargs="*",
                        help="текст запроса; без них включается режим диалога")
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--show-pool", type=int, default=8,
                        help="сколько конечных кодов раскрыть у первого кандидата")
    args = parser.parse_args()

    started = time.time()
    progress("загружаю каталог (706 МБ, около 10 секунд)…")
    data = load_snapshot(CATALOG_DIR)
    progress(f"каталог загружен за {time.time() - started:.1f} с")
    metadata = json.loads((MODEL_DIR / "model.meta.json").read_text(encoding="utf-8"))
    progress("загружаю модель на GPU (448 МБ, около 6 секунд)…")
    encoder = E5Encoder(MODEL_DIR, metadata["revision"])
    progress(f"модель на устройстве {encoder.device}")
    search = Search(data, encoder)
    print(f"каталог: {len(data.catalog.categories)} поисковых документов, "
          f"{len(data.catalog.positions)} позиций | устройство: {encoder.device} | "
          f"загрузка {time.time() - started:.1f} с\n")

    if not args.queries:
        print("Режим диалога: каталог уже загружен, запросы считаются без ожидания.")
        print("Пустая строка или «выход» — конец.\n")
        while True:
            try:
                query = input("запрос> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not query or query.lower() in ("выход", "exit", "quit"):
                break
            report(search, data, query, args.top, args.show_pool)
        return 0

    for query in args.queries:
        report(search, data, query, args.top, args.show_pool)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
