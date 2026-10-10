"""Deterministic Category map for the clean (5) supply.

This module builds the missing piece named in docs/ML_INTEGRATION.md: a verified
map of analysis areas. The backend adapter (app/clean_supply.py) deliberately
creates no categories, and app/prepare.py only indexes what is already there.

Grouping rule — explicit parentPositionInfo links, NOT similar names:

    family head = parent_code, if present, else the position's own code
    family      = head + all positions whose parent_code == head

Every position lands in exactly one family, so no code is lost and no code is
duplicated. Heuristic `category` labels from the supply (одежда / мебель /
инструменты) are NOT used here: they are ranking hints, not types.

What `link_verified` asserts. app/analysis.py reads it as `type_verified`, so the
meaning must be stated precisely. We assert the STRUCTURAL claim and write it into
`evidence`: the scope of the category equals the set produced by explicit XML
parent links, all parents are templates, there are no cycles or dangling links
(guaranteed by Catalog's own validator), and every terminal position of the family
is enumerated. We do NOT assert a human-reviewed semantic claim that the family is
a correct commercial type. A name group is never promoted to a verified type on
the grounds of textual similarity.

Usage (offline; the index is built afterwards by app/prepare.py):

    python -m app.category_map <clean-supply-dir> <catalog-dir> [--model-dir DIR]
    python -m app.prepare index <catalog-dir> --model-dir <model-dir>
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from app.catalog import Catalog, Category, Snapshot, digest
from app.clean_supply import load_clean_supply

# Поисковая запись должна укладываться в 512 токенов E5; prepare это проверяет
# и падает, поэтому обрезаем заранее и детерминированно.
MAX_TOKENS = 512
DESCRIPTION_BUDGET_CHARS = 700
MAX_CHARACTERISTIC_NAMES = 30


def family_head(parent_code: str | None, code: str) -> str:
    """Голова семьи: явный родитель, иначе сама позиция."""
    return parent_code or code


def _parts(head, children, raw: dict, description_chars: int) -> list[str]:
    """Части поисковой записи в порядке приоритета (важное — первым)."""
    parts: list[str] = []
    if (head.name or "").strip():
        parts.append(head.name.strip())
    okpd = raw.get("okpd2") or {}
    if okpd.get("name"):
        parts.append(str(okpd["name"]))
    if okpd.get("code"):
        parts.append("ОКПД2 " + str(okpd["code"]))
    rubrics = [r.get("name") for r in raw.get("rubricators") or [] if r.get("name")]
    if rubrics:
        parts.append("Раздел: " + ", ".join(rubrics))
    description = (raw.get("nsi_description") or "").strip()
    if description:
        parts.append(description[:description_chars].rstrip())
    names: list[str] = []
    seen: set[str] = set()
    for position in [head, *children]:
        for prop in position.properties:
            key = prop.name.strip().casefold()
            if key and key not in seen:
                seen.add(key)
                names.append(prop.name.strip())
            if len(names) >= MAX_CHARACTERISTIC_NAMES:
                break
        if len(names) >= MAX_CHARACTERISTIC_NAMES:
            break
    if names:
        parts.append("Характеристики: " + "; ".join(names))
    return parts


def fit_text(parts: list[str], count, limit: int) -> tuple[str, bool]:
    """Собрать текст и гарантированно уложить его в лимит токенов.

    Порядок действий детерминированный: сначала отбрасываем описательную часть
    (предпоследнюю), затем имена характеристик, затем укорачиваем описание,
    и в крайнем случае обрезаем текст по символам. Название не теряется.
    """
    text = " | ".join(part for part in parts if part)
    if count(text) <= limit:
        return text, False

    dropped = False
    # 1) описание и характеристики — по одному, с конца приоритета
    while len(parts) > 1 and count(" | ".join(parts)) > limit:
        parts = parts[:-1]
        dropped = True
    text = " | ".join(part for part in parts if part)
    if count(text) <= limit:
        return text, dropped

    # 2) крайний случай: название само длиннее лимита — режем по символам
    base = parts[0]
    low, high = 1, len(base)
    while low < high:
        mid = (low + high + 1) // 2
        if count(base[:mid]) <= limit:
            low = mid
        else:
            high = mid - 1
    return base[:low].rstrip(), True


def search_text_for(head, children, raw: dict, *, count=None, limit: int = MAX_TOKENS - 4,
                    description_chars: int = DESCRIPTION_BUDGET_CHARS) -> tuple[str, bool]:
    """Поисковая запись семьи: название, ОКПД2, рубрикаторы, описание, имена характеристик.

    Значения и числа НЕ включаются: они служат для фильтрации (этап C), а не для
    определения типа товара.
    """
    parts = _parts(head, children, raw, description_chars)
    if count is None:
        # Без токенизатора — грубая оценка по символам (≈3 символа на токен для русского).
        count = lambda text: len(text) // 3 + 1  # noqa: E731
        limit = min(limit, 1500)
    return fit_text(parts, count, limit)


def compact_property_raw(catalog: Catalog) -> Catalog:
    """Replace each Property.raw_json with a reference into Position.raw_json.

    Property.raw_json is never read at runtime (verified: the field is only written in
    clean_supply and read nowhere), while it duplicates the characteristic object that
    already sits inside Position.raw_json. The reference keeps provenance: it names
    the position code and the characteristic code, so the original stays reachable.
    """
    positions = []
    for position in catalog.positions:
        properties = tuple(
            prop.model_copy(update={"raw_json": json.dumps(
                {"ref": f"{position.code}#{prop.id}", "origin": "position.raw_json"},
                ensure_ascii=False, separators=(",", ":"))})
            for prop in position.properties
        )
        positions.append(position.model_copy(update={"properties": properties}))
    return catalog.model_copy(update={"positions": tuple(positions)})


def position_signature(position) -> tuple[str, list] | None:
    """Каноническая подпись ограничений позиции или None, если семантика неясна.

    Сравнивать можно только подтверждённое: id характеристики (внутри семьи он общий,
    проверено), kind, обязательность, вид и значения с границами и единицами. Если
    свойство неподдержано, обязательность неизвестна или правило пустое — позиция
    НЕ участвует в сравнении: неразличимость в этом случае недоказуема.
    """
    parts: list[tuple] = []
    for prop in sorted(position.properties, key=lambda item: item.id):
        if prop.value_type == "unsupported" or prop.required is None or not prop.allowed:
            return None
        items: list[tuple] = []
        for allowed in prop.allowed:
            unit = allowed.okei_code or allowed.raw_unit or ""
            if allowed.kind == "enum":
                items.append(("enum", allowed.value or "", unit))
            elif allowed.kind == "number":
                items.append(("number", allowed.value or "", unit))
            else:
                items.append(("interval", allowed.lower or "", allowed.upper or "",
                              bool(allowed.lower_inclusive), bool(allowed.upper_inclusive), unit))
        parts.append((prop.id, prop.kind or "", bool(prop.required), prop.value_type,
                      tuple(sorted(items))))
    if not parts:
        return None
    digest = hashlib.sha256(
        json.dumps(parts, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    return digest, parts


def indistinguishable_groups(catalog: Catalog) -> tuple[list[dict], dict]:
    """Группы конечных позиций одной семьи с совпадающими ограничениями.

    Нужны алгоритму вопросов: по одинаковым ограничениям уточнять значения бессмысленно.
    Позиции с неясной семантикой в сравнение не берутся и считаются отдельно.
    """
    positions = {position.code: position for position in catalog.positions}
    families: dict[str, list] = defaultdict(list)
    for position in catalog.positions:
        if position.is_template is False and position.parent_code:
            families[position.parent_code].append(position)

    groups: list[dict] = []
    skipped_unknown = 0
    compared = 0
    for head in sorted(families):
        buckets: dict[str, list] = defaultdict(list)
        for position in sorted(families[head], key=lambda item: item.code):
            signed = position_signature(position)
            if signed is None:
                skipped_unknown += 1
                continue
            compared += 1
            buckets[signed[0]].append(position.code)
        for signature, codes in sorted(buckets.items()):
            if len(codes) < 2:
                continue
            groups.append({
                "family_id": head,
                "family_name": positions[head].name,
                "signature": signature,
                "count": len(codes),
                "codes": codes,
                "basis": "property id (shared inside the family), kind, required, "
                         "value type, values with bounds and units",
            })
    report = {
        "groups": len(groups),
        "positions_in_groups": sum(group["count"] for group in groups),
        "positions_compared": compared,
        "positions_skipped_unknown_semantics": skipped_unknown,
        "largest_group": max((group["count"] for group in groups), default=0),
    }
    return groups, report


def build_categories(catalog: Catalog, *, tokenizer=None, verbose: bool = False) -> tuple[tuple[Category, ...], dict]:
    """Построить карту типов. Возвращает (categories, report)."""
    # Счётчик токенов обязан совпадать с тем, что использует app/prepare.py:
    # AutoTokenizer.encode с префиксом и спецтокенами.
    count = (lambda text: len(tokenizer.encode("passage: " + text))) if tokenizer is not None else None
    positions = {position.code: position for position in catalog.positions}
    children: dict[str, list] = defaultdict(list)
    for position in catalog.positions:
        if position.parent_code:
            children[position.parent_code].append(position)

    heads: dict[str, list] = defaultdict(list)
    for position in catalog.positions:
        heads[family_head(position.parent_code, position.code)].append(position)

    categories: list[Category] = []
    truncated: list[str] = []
    by_head_kind = {"template": 0, "standalone_terminal": 0, "template_without_children": 0}
    terminal_covered: set[str] = set()
    terminal_total = 0

    for head_code in sorted(heads):
        members = heads[head_code]
        head = positions[head_code]
        kids = sorted(children.get(head_code, []), key=lambda p: p.code)
        kind = ("template" if head.is_template else "standalone_terminal")
        if head.is_template and not kids:
            kind = "template_without_children"
        by_head_kind[kind] += 1

        raw: dict = {}
        try:
            raw = json.loads(head.raw_json)
        except (TypeError, ValueError):
            raw = {}

        text, dropped = search_text_for(head, kids, raw, count=count, limit=MAX_TOKENS - 4)
        if dropped:
            truncated.append(head_code)
        # Контроль тем же способом, каким его делает app/prepare.py.
        if count is not None and count(text) > MAX_TOKENS:
            raise ValueError(f"Поисковая запись не уложилась в лимит: {head_code}")

        terminal_children = [p for p in kids if p.is_template is False]
        terminal_covered.update(p.code for p in terminal_children)
        if head.is_template is False:
            terminal_covered.add(head.code)

        complete = bool(terminal_children) or head.is_template is False
        evidence_parts = [
            f"structural family from parentPositionInfo: head={head_code}",
            f"head_is_template={head.is_template}",
            f"terminal_children={len(terminal_children)}",
            "parents validated by Catalog (exists, is template, no cycles)",
        ]
        if complete:
            evidence_parts.append("all terminal positions of the family enumerated")
        else:
            evidence_parts.append("template has no children: no terminal positions to analyse")

        categories.append(Category(
            id=head_code,
            name=head.name,
            search_text=text,
            position_codes=(head_code,),
            link_verified=True,
            coverage_verified=complete,
            evidence="; ".join(evidence_parts),
        ))

    terminal_total = sum(1 for p in catalog.positions if p.is_template is False)
    missing = sorted({p.code for p in catalog.positions if p.is_template is False} - terminal_covered)

    report = {
        "positions_total": len(catalog.positions),
        "templates": by_head_kind["template"] + by_head_kind["template_without_children"],
        "terminal_positions": terminal_total,
        "categories": len(categories),
        "by_head_kind": by_head_kind,
        "categories_with_complete_coverage": sum(1 for c in categories if c.coverage_verified),
        "terminal_positions_covered": len(terminal_covered),
        "terminal_positions_missing": missing[:20],
        "terminal_positions_missing_count": len(missing),
        "search_text_truncated": len(truncated),
        "search_text_truncated_examples": truncated[:10],
        "max_search_text_chars": max((len(c.search_text) for c in categories), default=0),
    }
    if verbose:
        for key, value in report.items():
            print(f"  {key}: {value}")
    return tuple(categories), report


def build(clean_dir: Path, catalog_dir: Path, model_dir: Path | None,
          compact_raw: bool = False) -> int:
    catalog_dir.mkdir(parents=True, exist_ok=True)
    print(f"читаю поставку: {clean_dir}")
    snap: Snapshot = load_clean_supply(clean_dir)
    catalog = snap.catalog
    print(f"позиций в поставке: {len(catalog.positions)}")

    tokenizer = None
    if model_dir is not None and (model_dir / "tokenizer.json").exists():
        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(model_dir, local_files_only=True, trust_remote_code=False)
        print("токенизатор модели загружен, длина записей контролируется")
    else:
        print("токенизатор недоступен: длина записей контролируется по символам")

    categories, report = build_categories(catalog, tokenizer=tokenizer, verbose=True)
    if report["terminal_positions_missing_count"]:
        raise ValueError(f"Не все конечные позиции покрыты: {report['terminal_positions_missing_count']}")

    enriched = catalog.model_copy(update={"categories": categories})
    if compact_raw:
        before = len(json.dumps(enriched.model_dump(), ensure_ascii=False))
        enriched = compact_property_raw(enriched)
        after = len(json.dumps(enriched.model_dump(), ensure_ascii=False))
        report["raw_json_compaction"] = {
            "applied": True,
            "serialized_before_chars": before,
            "serialized_after_chars": after,
            "ratio": round(after / max(before, 1), 3),
        }
        print(f"  raw_json характеристики сведён к ссылке: "
              f"{before/1e6:.1f} → {after/1e6:.1f} млн символов")
    # Повторная валидация тем же классом, что использует backend.
    enriched = Catalog.model_validate(enriched.model_dump())

    groups, group_report = indistinguishable_groups(enriched)
    report["indistinguishable"] = group_report
    with (catalog_dir / "indistinguishable_groups.jsonl").open("w", encoding="utf-8") as fh:
        for group in groups:
            fh.write(json.dumps(group, ensure_ascii=False) + "\n")
    print(f"  групп неразличимых конечных позиций: {group_report['groups']} "
          f"({group_report['positions_in_groups']} позиций); "
          f"пропущено из-за неясной семантики: {group_report['positions_skipped_unknown_semantics']}")

    (catalog_dir / "catalog.json").write_text(enriched.model_dump_json(), encoding="utf-8")

    report["catalog_sha256"] = digest(catalog_dir / "catalog.json")
    report["catalog_version"] = enriched.catalog_version
    report["source_files"] = {
        name: hashlib.sha256((clean_dir / name).read_bytes()).hexdigest()
        for name in ("ktru_actual_all.jsonl.gz", "okei_units.json", "example_unit_strings.json",
                     "source_issues.json", "validation_report.json", "category_rules.json")
        if (clean_dir / name).exists()
    }
    (catalog_dir / "category_map_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"catalog.json записан: {catalog_dir / 'catalog.json'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Category (type) map for the clean supply")
    parser.add_argument("clean_dir", type=Path)
    parser.add_argument("catalog_dir", type=Path)
    parser.add_argument("--model-dir", type=Path, default=None)
    parser.add_argument("--compact-raw", action="store_true",
                        help="свести raw_json характеристики к ссылке на raw_json позиции")
    args = parser.parse_args()
    return build(args.clean_dir, args.catalog_dir, args.model_dir, compact_raw=args.compact_raw)


if __name__ == "__main__":
    raise SystemExit(main())
