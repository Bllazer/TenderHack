"""Проверка поставки ML-артефактов на реальных данных.

Запуск (из каталога backend):
    python tests/verify_real_supply.py

Проверяет пункт 9 задания: связи, порядок, нормализацию, поиск по названию,
перефразировке и опечатке, а также попадание ожидаемого конечного кода в пул,
раскрытый из top-k поисковых документов. Ожидания заданы на уровне семьи и
проверяются по факту: код должен оказаться в пуле одной из верхних категорий.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.catalog import load_snapshot  # noqa: E402
from app.search import E5Encoder, Search  # noqa: E402

CATALOG_DIR = Path(os.environ.get("VERIFY_CATALOG_DIR", "resources/catalog-build"))
MODEL_DIR = Path(os.environ.get("VERIFY_MODEL_DIR", "resources/model"))

# Запрос -> ожидаемый конечный код (проверено отбором по каталогу).
QUERIES: list[tuple[str, str, str]] = [
    ("шкаф для документов деревянный", "31.01.12.139-00000001", "название"),
    ("кресло офисное", "31.01.12.160-00000005", "название"),
    ("шуруповерт", "28.24.11.000-00000030", "название"),
    ("дрель ручная электрическая от сети", "28.24.11.000-00000001", "название"),
    ("рюкзак туристический", "15.12.12.192-00000001", "название"),
    ("перчатки нитриловые неопудренные", "22.19.60.119-00000011", "название"),
    ("мебель для хранения бумаг с замком", "31.01.12.139-00000001", "перефразировка"),
    ("шуруповирт аккумуляторный", "28.24.11.000-00000030", "опечатка"),
]


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"  — {detail}" if detail else ""))
    return ok


def main() -> int:
    failures = 0

    print("=" * 78)
    print("1. ЗАГРУЗКА И СВЯЗНОСТЬ")
    print("=" * 78)
    started = time.time()
    data = load_snapshot(CATALOG_DIR)
    load_seconds = time.time() - started
    failures += not check("snapshot загружен без проблемы",
                          data.problem is None and not data.missing_resources,
                          f"problem={data.problem}, missing={data.missing_resources}")
    positions = {p.code: p for p in data.catalog.positions}
    categories = {c.id: c for c in data.catalog.categories}
    failures += not check("идентификаторы уникальны",
                          len(positions) == len(data.catalog.positions)
                          and len(categories) == len(data.catalog.categories))

    terminal = {code for code, p in positions.items() if p.is_template is False}
    template = {code for code, p in positions.items() if p.is_template is True}
    covered: set[str] = set()
    for positions_pool, _ in data.scopes.values():
        covered.update(p.code for p in positions_pool)
    failures += not check("все конечные позиции достижимы через области",
                          terminal <= covered,
                          f"потеряно {len(terminal - covered)}")
    failures += not check("шаблоны не попадают в пул как итоговые коды",
                          not (covered & template))
    dangling = [p.code for p in data.catalog.positions
                if p.parent_code and p.parent_code not in positions]
    failures += not check("нет нарушенных ссылок на родителя", not dangling,
                          f"{len(dangling)}")

    print()
    print("=" * 78)
    print("2. ИНДЕКС И ПОРЯДОК")
    print("=" * 78)
    matrix = data.vectors
    failures += not check("размерность матрицы = числу категорий",
                          matrix is not None and matrix.shape == (len(data.catalog.categories), 384),
                          f"{None if matrix is None else matrix.shape}")
    failures += not check("векторы конечны", bool(np.isfinite(matrix).all()))
    norms = np.linalg.norm(matrix, axis=1)
    failures += not check("векторы нормированы",
                          bool(np.allclose(norms, 1, atol=1e-3)),
                          f"мин {norms.min():.5f}, макс {norms.max():.5f}")
    failures += not check("токены выровнены с категориями",
                          len(data.tokens) == len(data.catalog.categories))
    failures += not check("revision модели закреплён 40 символами",
                          bool(data.model_revision) and len(data.model_revision) == 40,
                          str(data.model_revision))
    manifest = json.loads((CATALOG_DIR / "manifest.json").read_text(encoding="utf-8"))
    failures += not check("manifest перечисляет все три артефакта",
                          set(manifest["sha256"]) == {"catalog.json", "vectors.npy", "tokens.json"})

    print()
    print("=" * 78)
    print("3. ПОИСК (BM25 + dense + char n-gram + RRF)")
    print("=" * 78)
    metadata = json.loads((MODEL_DIR / "model.meta.json").read_text(encoding="utf-8"))
    encoder = E5Encoder(MODEL_DIR, metadata["revision"])
    print(f"  устройство энкодера: {getattr(encoder, 'device', 'неизвестно')}, "
          f"батч {getattr(encoder, 'batch_size', '?')}")
    search = Search(data, encoder)
    print(f"  char n-gram канал: {'включён' if search.char else 'выключен'}"
          + (f" ({search.char.grams_total} постингов)" if search.char else ""))

    hits = 0
    latencies: list[float] = []
    for query, expected, kind in QUERIES:
        started = time.time()
        candidates = search.find(query)
        latencies.append((time.time() - started) * 1000)
        pool: set[str] = set()
        for candidate in candidates:
            positions_pool, _ = data.scopes[candidate.id]
            pool.update(p.code for p in positions_pool)
        found = expected in pool
        hits += found
        problems = [c.id for c in candidates if data.scopes[c.id][1] is False]
        print(f"  {'OK  ' if found else 'MISS'} [{kind:14}] {query!r}")
        print(f"        ожидался {expected}; top: " +
              ", ".join(f"{c.id}({c.rank_score:.4f})" for c in candidates[:3]) +
              (f"  [неполные области: {len(problems)}]" if problems else ""))
    failures += not check("ожидаемый код найден в пуле top-5",
                          hits == len(QUERIES), f"{hits} из {len(QUERIES)}")
    latencies.sort()
    print(f"  задержка запроса: p50 {latencies[len(latencies)//2]:.0f} мс, "
          f"макс {latencies[-1]:.0f} мс")

    print()
    print("=" * 78)
    print("4. ОСОБЫЕ СЛУЧАИ")
    print("=" * 78)
    no_chars = [p for p in data.catalog.positions
                if p.is_template is False and not p.properties]
    blocked = [p for p in data.catalog.positions if p.resolve_blocked]
    issues = [p for p in data.catalog.positions if p.source_issues]
    failures += not check("позиции без характеристик сохраняют блокировку",
                          all(p.resolve_blocked for p in no_chars),
                          f"{len(no_chars)} позиций")
    failures += not check("source_issues сохранены", len(issues) == 7, f"{len(issues)}")
    failures += not check("нет карантинных позиций в рабочем каталоге",
                          not any(p.quarantined for p in data.catalog.positions))
    multiple = [p for p in data.catalog.positions
                if any(prop.kind == "3" for prop in p.properties)]
    failures += not check("kind=3 сохранён и не превращён в конфликт",
                          bool(multiple), f"{len(multiple)} позиций с kind=3")
    unsupported = [p for p in data.catalog.positions
                   if any(prop.value_type == "unsupported" for prop in p.properties)]
    print(f"  позиций с неподдержанными ограничениями: {len(unsupported)}")

    print()
    print("=" * 78)
    print(f"ИТОГ: {'все проверки пройдены' if failures == 0 else f'провалено проверок: {failures}'}")
    print(f"загрузка snapshot: {load_seconds:.1f} с")
    print("=" * 78)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
