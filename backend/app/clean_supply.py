"""Thin adapter of clean (5) JSON; no XML conversion, taxonomy or index generation."""
import gzip
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.catalog import (AllowedValue, Catalog, Position, Property, UnitAlias, UnitDefinition,
                         digest, snapshot, validate_allowed)

# Explicit supported physical conversions only. Unknown OKEI stays unsupported.
# Codes verified against the ОКЕИ reference. Two of them were wrong and silently
# accepted: 050 is the square millimetre, not the square centimetre (051), and 271
# is the joule, not the ohm (274). Both mapped onto a name app.units knows, so the
# value passed as verified while carrying a unit of the wrong dimension or scale.
# Every name here must resolve in app.units.get_unit, otherwise the value is blocked
# as unit_unknown, which is why 050 and 271 required matching entries there too.
OKEI_SUPPORTED = {"003": "мм", "004": "см", "006": "м", "050": "мм²", "051": "см²", "055": "м²",
                  "111": "мл", "112": "л", "113": "м³", "163": "г", "166": "кг", "212": "Вт",
                  "214": "кВт", "222": "В", "271": "Дж", "274": "Ом", "280": "°C",
                  "331": "об/мин", "354": "с", "355": "мин", "642": "ед", "715": "пар",
                  "744": "%", "796": "шт"}


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def adapt_property(row, origin):
    values = []
    unsupported = []
    if row.get("kind") not in ("1", "2"):
        unsupported.append("multiple_choice_not_supported" if row.get("kind") == "3" else "unknown_kind")
    if row.get("type") not in ("1", "2"):
        unsupported.append("unknown_type")
    for item in row.get("values", []):
        okei = item.get("okei") or {}
        base = {"unit": OKEI_SUPPORTED.get(okei.get("code"), okei.get("name")), "raw_unit": okei.get("name"),
                "okei_code": okei.get("code"), "value_code": item.get("value_code"),
                "source": origin + "/" + str(row.get("code")) + "/" + str(item.get("value_code"))}
        alternatives = []
        if row.get("type") == "1" and item.get("quality_description"):
            alternatives.append({**base, "kind": "enum", "value": item["quality_description"]})
        elif row.get("type") == "2":
            if not okei.get("code"):
                unsupported.append("missing_source_unit")
            elif okei["code"] not in OKEI_SUPPORTED:
                unsupported.append("unsupported_source_unit")
            for concrete in item.get("concrete_values", []):
                try:
                    number = Decimal(str(concrete).replace(",", "."))
                    alternatives.append({**base, "kind": "number", "value": str(number)})
                except InvalidOperation:
                    unsupported.append("invalid_source_number")
            for interval in item.get("ranges", []):
                lo, hi = interval.get("min"), interval.get("max")
                left, right = interval.get("min_notation"), interval.get("max_notation")
                if (lo is not None and left not in ("greater", "greaterOrEqual")) or (hi is not None and right not in ("less", "lessOrEqual")):
                    unsupported.append("unsupported_operator")
                    continue
                alternatives.append({**base, "kind": "interval", "lower": str(lo) if lo is not None else None,
                                     "upper": str(hi) if hi is not None else None,
                                     "lower_inclusive": left != "greater", "upper_inclusive": right != "less"})
        if not alternatives:
            unsupported.append("unsupported_value")
        for alternative in alternatives:
            try:
                value = AllowedValue.model_validate(alternative)
                validate_allowed(value)
                values.append(value)
            except ValueError:
                unsupported.append("invalid_source_constraint")
    # Entire rule is unsupported if any alternative cannot be represented; originals stay in raw_json.
    return Property(id=row["code"], name=row["name"], source_type=row.get("type"), kind=row.get("kind"),
                    choice_type=row.get("choice_type"), required=row.get("is_required"), evidence=origin,
                    value_type=("text" if row["type"] == "1" else "number") if not unsupported else "unsupported",
                    verified=not unsupported, allowed=tuple(values), raw_json=encode(row),
                    unsupported_reasons=tuple(sorted(set(unsupported))))


def adapt_position(row, origin, original=None):
    return Position(code=row["code"], name=row["name"], version=str(row["version"]), source_status=row["status"],
                    status="actual" if row["status"] == "ACTIVE" else "unknown",
                    parent_code=(row.get("parent_position") or {}).get("code"), is_template=row.get("is_template"),
                    okpd2=(row.get("okpd2") or {}).get("code"), no_new_features=row.get("no_new_features"),
                    no_new_features_evidence=row.get("no_new_features_reason"), source_path=origin,
                    raw_json=original or encode(row), source_issues=tuple(encode(item) for item in row.get("source_issues", [])),
                    resolve_blocked=row.get("resolve_blocked"), resolve_blocked_reason=tuple(row.get("resolve_blocked_reason") or []),
                    properties=tuple(adapt_property(prop, origin) for prop in row["characteristics"]))


def read_auxiliary(directory):
    names = ("okei_units.json", "example_unit_strings.json", "source_issues.json", "validation_report.json", "category_rules.json")
    auxiliary = {name: json.loads((directory / name).read_text(encoding="utf-8")) for name in names}
    units = tuple(UnitDefinition.model_validate({"code": u["code"], "spellings": u["spellings"], "usage_total": u["usage_total"]})
                  for u in auxiliary["okei_units.json"]["units"])
    targets = {(u.code, spelling.name) for u in units for spelling in u.spellings}
    if any(sum(s.usage for s in u.spellings) != u.usage_total for u in units):
        raise ValueError("Unit spelling usage mismatch")
    aliases = []
    for row in auxiliary["example_unit_strings.json"]["aliases"]:
        if (row["okei_code"], row["okei_name"]) not in targets:
            raise ValueError("Dangling alias target")
        aliases.append(UnitAlias(raw=row["raw"], okei_code=row["okei_code"], okei_name=row["okei_name"],
                                 global_safe=row["global_safe"], context=row.get("context"), raw_json=encode(row)))
    return auxiliary, units, tuple(aliases), {name: digest(directory / name) for name in names}


def supply_version(hashes):
    import hashlib
    return "clean5-" + hashlib.sha256(encode(dict(sorted(hashes.items()))).encode()).hexdigest()[:16]


def expected_supply_version(directory: Path):
    _, _, _, hashes = read_auxiliary(directory)
    source = directory / "ktru_actual_all.jsonl.gz"
    hashes[source.name] = digest(source)
    return supply_version(hashes)


def load_clean_supply(directory: Path):
    auxiliary, units, aliases, hashes = read_auxiliary(directory)
    source = directory / "ktru_actual_all.jsonl.gz"
    hashes[source.name] = digest(source)
    positions = []
    with gzip.open(source, "rt", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            row = json.loads(line, parse_float=Decimal)
            origin = f"{source.name}:sha256:{hashes[source.name]}:line:{line_number}#{row['code']}@{row['version']}"
            positions.append(adapt_position(row, origin, line.rstrip("\r\n")))
    catalog = Catalog(catalog_version=supply_version(hashes), source_description="Local clean (5), full actual corpus",
                      validation_report=encode(hashes), positions=tuple(positions), units=units, unit_aliases=aliases,
                      auxiliary_json=encode(auxiliary))
    report = auxiliary["validation_report.json"]["files"][source.name]
    actual = {"positions": len(positions), "templates": sum(p.is_template is True for p in positions),
              "characteristics": sum(len(p.properties) for p in positions),
              "parent_links": sum(p.parent_code is not None for p in positions),
              "final_positions_without_characteristics": sum(p.is_template is False and not p.properties for p in positions),
              "positions_with_source_issues": sum(bool(p.source_issues) for p in positions)}
    if any(report[key] != count for key, count in actual.items()):
        raise ValueError("Supply counts disagree with validation report")
    if {p.code for p in positions if p.source_issues} != set(auxiliary["source_issues.json"]["position_codes"]):
        raise ValueError("Source issue coverage mismatch")
    # No fabricated taxonomy or vectors. ML artifacts must supply the existing Category IDs/schema and index manifest.
    return snapshot(catalog, problem="search_index_missing", missing_resources=("category_mapping", "search_index"))
