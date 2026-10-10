"""Диагностика retrieval: фактический текст документов и разбор по каналам.

Показывает, что именно уходит в BM25 и в эмбеддер, и раскладывает результат на
три отдельных канала (BM25, dense, RRF), чтобы видеть, где теряется нужный код.
Комбинация RRF здесь повторяет app/search.Search.find один в один.

Запуск (из каталога backend):
    python tests/diagnose_retrieval.py [--catalog DIR] [--model-dir DIR]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.catalog import load_snapshot  # noqa: E402
from app.search import E5Encoder, Search  # noqa: E402

# Смотрим на конкретные семейства: головной код -> что это
DOCUMENTS = [
    ("28.24.11.000-00000003", "бытовая электрическая дрель (шаблон семейства)"),
    ("32.50.50.190-00001233", "двигатель хирургической дрели"),
    ("31.01.12.139-00000002", "шкаф для документов (шаблон семейства)"),
]

# Запрос -> ожидаемый конечный код
QUERIES = [
    ("дрель ручная электрическая от сети", "28.24.11.000-00000001", "название"),
    ("шуруповерт", "28.24.11.000-00000030", "название"),
    ("шкаф для документов деревянный", "31.01.12.139-00000001", "название"),
    ("мебель для хранения бумаг с замком", "31.01.12.139-00000001", "перефразировка MISS"),
    ("шуруповирт аккумуляторный", "28.24.11.000-00000030", "опечатка MISS"),
]


def family_of(code: str, positions: dict) -> str:
    position = positions.get(code)
    if position is None:
        return code
    return position.parent_code or code


def rrf_combine(channels: dict[str, np.ndarray], categories, k0: int = 60, depth: int = 50):
    """Повторяет Search.find: топ-50 из каждого канала, 1/(k0+rank)."""
    ranks: dict[int, float] = {}
    sources: dict[int, list[str]] = {}
    per_channel: dict[str, list[int]] = {}
    for label, scores in channels.items():
        if scores is None:
            continue
        order = np.argsort(-scores, kind="stable")[:depth]
        per_channel[label] = [int(i) for i in order]
        for rank, index in enumerate(order, 1):
            if label in ("BM25", "CHAR") and scores[index] <= 0:
                continue
            ranks[int(index)] = ranks.get(int(index), 0) + 1 / (k0 + rank)
            sources.setdefault(int(index), []).append(label)
    combined = sorted(ranks, key=lambda i: (-ranks[i], categories[i].id))[:5]
    return combined, ranks, sources, per_channel


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", default="resources/catalog-build")
    parser.add_argument("--model-dir", default="resources/model")
    args = parser.parse_args()

    data = load_snapshot(Path(args.catalog))
    if data.problem:
        print(f"snapshot problem: {data.problem}")
    positions = {p.code: p for p in data.catalog.positions}
    categories = data.catalog.categories
    by_id = {c.id: c for c in categories}

    print("=" * 100)
    print("1. ФАКТИЧЕСКИЙ ТЕКСТ ДОКУМЕНТОВ")
    print("=" * 100)
    metadata = json.loads((Path(args.model_dir) / "model.meta.json").read_text(encoding="utf-8"))
    encoder = E5Encoder(Path(args.model_dir), metadata["revision"])
    tokenizer = encoder.tokenizer
    for code, label in DOCUMENTS:
        category = by_id.get(code)
        if category is None:
            print(f"\n[{code}] НЕТ такой категории — {label}")
            continue
        text = category.search_text
        tokens = len(tokenizer.encode("passage: " + text))
        print(f"\n[{code}] {label}")
        print(f"  категория: {category.name!r}")
        print(f"  позиций в области: {len(category.position_codes)}, "
              f"link_verified={category.link_verified}, coverage_verified={category.coverage_verified}")
        print(f"  токенов: {tokens} из 512; символов: {len(text)}")
        print(f"  ТЕКСТ:\n    {text[:1200]}")
        if len(text) > 1200:
            print(f"    … ещё {len(text)-1200} символов")
        first = text.split(" | ")[0]
        print(f"  первая часть (название): {first!r}")
        print(f"  позиция названия в тексте: 0..{len(first)} из {len(text)} "
              f"({100*len(first)/max(1,len(text)):.0f} % длины)")

    print()
    print("=" * 100)
    print("2. РАЗБОР ПО КАНАЛАМ")
    print("=" * 100)
    search = Search(data, encoder)
    for query, expected, kind in QUERIES:
        head = family_of(expected, positions)
        print(f"\nЗАПРОС [{kind}]: {query!r}")
        print(f"  ожидаемый конечный код: {expected}; его семья (категория): {head}")
        started = time.time()
        lexical = search.lexical(query)
        dense = data.vectors @ encoder.encode([query], "query: ")[0]
        char = search.char.scores(query) if search.char else None
        elapsed = (time.time() - started) * 1000
        combined, ranks, sources, per_channel = rrf_combine(
            {"BM25": lexical, "E5": dense, "CHAR": char}, categories)

        for label, key, scores in (("BM25", "BM25", lexical), ("DENSE", "E5", dense),
                                   ("CHAR", "CHAR", char)):
            if scores is None:
                continue
            order = per_channel.get(key, [])
            parts = []
            for rank, index in enumerate(order[:5], 1):
                mark = " <== ОЖИДАЕМАЯ" if categories[index].id == head else ""
                parts.append(f"{rank}. {categories[index].id}({scores[index]:.4f}){mark}")
            head_rank = next((r for r, i in enumerate(order, 1) if categories[i].id == head), None)
            print(f"  {label:6} top-5: " + ("  ".join(parts) if parts else "(пусто)"))
            print(f"         ранг ожидаемой семьи: {head_rank if head_rank else 'вне топ-50'}")

        parts = []
        for rank, index in enumerate(combined, 1):
            mark = " <== ОЖИДАЕМАЯ" if categories[index].id == head else ""
            parts.append(f"{rank}. {categories[index].id}({ranks[index]:.4f}){mark}")
        print(f"  RRF    top-5: " + "  ".join(parts))
        pool: set[str] = set()
        for index in combined:
            pool.update(p.code for p in data.scopes[categories[index].id][0])
        print(f"  ожидаемый код в пуле top-5: {'ДА' if expected in pool else 'НЕТ'}")
        print(f"  время каналов: {elapsed:.0f} мс")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
