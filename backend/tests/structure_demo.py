"""Показать формат структурированных фактов для алгоритма C на реальных данных.

Запуск из каталога backend:
    python tests/structure_demo.py

Печатает для одного и того же текста сначала текущий `Fact` (формат API и
фронтенда), затем `StructuredFact` — представление для C, и наконец то же
представление после привязки к характеристикам выбранной позиции.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.catalog import load_snapshot  # noqa: E402
from app.extraction import extract  # noqa: E402
from app.structured import to_structured  # noqa: E402

CATALOG_DIR = Path(os.environ.get("DEMO_CATALOG_DIR", "resources/catalog-build"))

TEXT = "Тип питания: От сети; мощность не менее 650 Вт; без наличия режима реверса"
COMMA_TEXT = "Мощность: 650 Вт, напряжение 220 В, тип патрона быстросъёмный"
POSITION = "28.24.11.000-00000001"


def show(text: str, names: tuple[str, ...], position) -> None:
    facts, unparsed = extract(text, names)
    print("=" * 90)
    print("ТЕКСТ:", text)
    print("=" * 90)

    print("\n1. ТЕКУЩИЙ Fact — формат API и фронтенда (не изменён)")
    for fact in facts:
        print(f"   name={fact.name!r}  value={fact.value!r}  unit={fact.unit!r}  negated={fact.negated}")

    print("\n2. StructuredFact ДЛЯ АЛГОРИТМА C, до выбора категории")
    for item in to_structured(facts):
        print("   " + json.dumps(item.model_dump(), ensure_ascii=False))

    print("\n3. StructuredFact ПОСЛЕ привязки к характеристикам позиции")
    for item in to_structured(facts, position):
        print("   " + json.dumps(item.model_dump(), ensure_ascii=False))

    print("\n4. ЧТО ИЗМЕНИЛОСЬ")
    for fact, item in zip(facts, to_structured(facts, position)):
        bound = item.def_id or "не сопоставлено (C подхватит по имени)"
        unit = item.unit.text if item.unit else None
        print(f"   {fact.name[:32]:32} kind {item.kind:8} оператор {str(item.operator):4} "
              f"значение {str(item.value)[:22]:22} единица {str(unit):6} def_id -> {bound}")

    if unparsed:
        print("\n   неразобранные фрагменты:", [f.raw.strip() for f in unparsed])
    print()


def main() -> int:
    data = load_snapshot(CATALOG_DIR)
    position = next((p for p in data.catalog.positions if p.code == POSITION), None)
    if position is None:
        print(f"позиция {POSITION} не найдена")
        return 1

    names = tuple({name for prop in position.properties for name in (prop.name, *prop.aliases)})
    print("ПОЗИЦИЯ:", position.code, "|", position.name)
    print("характеристики:", ", ".join(prop.name for prop in position.properties[:6]), "…\n")

    show(TEXT, names, position)

    print("=" * 90)
    print("РАЗДЕЛИТЕЛИ: «;», «|», перевод строки и запятая (кроме десятичной «1,5»)")
    print("=" * 90)
    show(COMMA_TEXT, names, position)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
