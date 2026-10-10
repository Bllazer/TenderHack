"""Offline source inspection, not a converter or runtime catalog loader."""
import argparse
from decimal import Decimal
import gzip
import hashlib
import io
import json
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path


def children(element):
    return {c.tag.rsplit("}", 1)[-1]: c for c in element}


def text(element):
    return " ".join((element.text or "").split()) if element is not None else None


def verify_characteristics(data, clean):
    fields = children(data)
    for key, xml in (("no_new_features", "noNewFeatures"), ("is_template", "isTemplate")):
        raw = text(fields.get(xml))
        expected = {"true":True,"false":False,None:None}.get(raw)
        assert expected == clean.get(key), (clean["code"], key)
    parent = fields.get("parentPositionInfo")
    if parent is not None:
        assert text(children(parent).get("code")) == (clean.get("parent_position") or {}).get("code")
    block = fields.get("characteristics")
    actual = {text(children(c).get("code")): c for c in block or [] if text(children(c).get("actual")) == "true"}
    assert set(actual) == {p["code"] for p in clean["characteristics"]}
    for prop in clean["characteristics"]:
        original = children(actual[prop["code"]])
        for xml, key in (("name","name"),("kind","kind"),("type","type"),("choiceType","choice_type")):
            assert text(original.get(xml)) == prop.get(key), (clean["code"], key)
        assert text(original.get("isRequired")) in ("true", "false"), "Requiredness absent in actual XML"
        assert (text(original.get("isRequired")) == "true") == prop["is_required"]
        for value in prop["values"]:
            candidates = [v for v in original.get("values", []) if text(children(v).get("valueCode")) == value["value_code"]]
            assert candidates
            source = children(candidates[0])
            assert text(source.get("qualityDescription")) == value.get("quality_description")
            unit = source.get("OKEI")
            assert (unit is not None) == bool(value.get("okei"))
            if unit is not None:
                for key in ("code", "name"):
                    assert text(children(unit).get(key)) == value["okei"].get(key)
            concrete = [text(v) for v in source.get("valueSet", [])]
            assert {Decimal(v.replace(",", ".")) for v in concrete} == {Decimal(str(v)) for v in value.get("concrete_values", [])}
            ranges = list(source.get("rangeSet", []))
            assert len(ranges) == len(value.get("ranges", []))
            for original_range, expected in zip(ranges, value.get("ranges", [])):
                fields = children(original_range)
                for bound in ("min", "max"):
                    raw = text(fields.get(bound))
                    assert (Decimal(raw) if raw else None) == (Decimal(str(expected[bound])) if expected.get(bound) is not None else None)
                    assert text(fields.get(bound + "MathNotation")) == expected.get(bound + "_notation")


def inspect(archive, selection, output):
    rows = json.loads(selection.read_text(encoding="utf-8"))
    wanted = {r["code"]: r for r in rows}
    output.mkdir(parents=True, exist_ok=True)
    found = {}
    with zipfile.ZipFile(archive) as outer:
        for member in outer.namelist():
            if not member.endswith(".zip"):
                continue
            with zipfile.ZipFile(io.BytesIO(outer.read(member))) as inner:
                for name in inner.namelist():
                    if "_actual_" not in name:
                        continue
                    with inner.open(name) as stream:
                        for _, element in ET.iterparse(stream, events=("end",)):
                            if element.tag.rsplit("}", 1)[-1] != "position":
                                continue
                            data = next((c for c in element if c.tag.endswith("}data")), None)
                            if data is not None:
                                fields = {c.tag.rsplit("}", 1)[-1]: (c.text or "").strip() for c in data}
                                code = fields.get("code")
                                if code in wanted and fields.get("version") == str(wanted[code]["version"]):
                                    verify_characteristics(data, wanted[code])
                                    xml = ET.tostring(element, encoding="utf-8")
                                    (output / (code + ".xml")).write_bytes(xml)
                                    chars = next((c for c in data if c.tag.endswith("}characteristics")), [])
                                    source = []
                                    for char in chars:
                                        values = {c.tag.rsplit("}", 1)[-1]: (c.text or "").strip() for c in char}
                                        if values.get("actual") == "true":
                                            source.append(values)
                                    found[code] = {"member": member, "actual_xml": name,
                                                   "sha256": hashlib.sha256(xml).hexdigest(), "fields": fields,
                                                   "characteristics": source, "clean_matches_actual_xml": True}
                                    print("FOUND", code, flush=True)
                            element.clear()
            if len(found) == len(wanted):
                break
    result = {"archive_sha256": hashlib.file_digest(archive.open("rb"), "sha256").hexdigest(),
              "positions": found, "missing": sorted(set(wanted) - set(found))}
    (output / "sources.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if result["missing"]:
        raise ValueError(result["missing"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("selection", type=Path)
    parser.add_argument("output", type=Path)
    a = parser.parse_args()
    inspect(a.archive, a.selection, a.output)
