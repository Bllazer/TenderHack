"""Live acceptance, real E5/source rules; no hardcoded expected code lists.

From backend: python tests/real_exclusion_stage1.py before|after
"""
import gzip
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

PHASE = sys.argv[1]
BASE = "http://localhost:8080"
SUPPLY = Path("../ktru_clean")
OUT = Path("docs/reviews/stage1")
CASES = [("лопата совковая", "Лопата"), ("молоток столярный", "Молоток"),
         ("пальто мужское", "Пальто зимнее из текстильных материалов"),
         ("пальто женское", "Пальто зимнее из текстильных материалов"),
         ("пальто мужское 48 размера", "Пальто зимнее из текстильных материалов")]


def run():
    records, roots = {}, {}
    names = {name for _, name in CASES}
    with gzip.open(SUPPLY / "ktru_actual_all.jsonl.gz", "rt", encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if row["name"] in names:
                records[row["code"]] = row
                if row["is_template"] is True:
                    assert row["name"] not in roots
                    roots[row["name"]] = row["code"]
    report = {"base": BASE, "phase": PHASE, "http": [], "cases": []}

    def fetch(path, body=None):
        req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            response = urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            result = json.load(response)
            report["http"].append({"method": "POST" if body is not None else "GET", "path": path,
                                    "request": body, "status_code": response.status, "response": result})
            return response.status, result

    assert fetch("/api/health/ready")[0] == 200
    assert fetch("/api/v1/catalog/status")[1]["ready"]
    if PHASE == "after":
        status, spec = fetch("/api/openapi.json")
        assert status == 200 and "exclusion_reasons" in spec["components"]["schemas"]["Candidate"]["properties"]
    report["manifest"] = json.loads(Path("resources/catalog-build/manifest.json").read_text(encoding="utf-8"))
    report["model_meta"] = json.loads(Path("resources/model/model.meta.json").read_text(encoding="utf-8"))
    if PHASE == "after":
        report["actual_artifact_sha256"] = {}
        for name, expected in report["manifest"]["sha256"].items():
            with (Path("resources/catalog-build") / name).open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            assert actual == expected
            report["actual_artifact_sha256"][name] = actual
    with (SUPPLY / "ktru_actual_all.jsonl.gz").open("rb") as stream:
        report["supply_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    cards = {}
    for description, name in CASES:
        category = roots[name]
        # A blank category baseline enumerates the full real pool; candidates are not an oracle.
        status, baseline = fetch("/api/v1/analyses", {"description":name, "confirmed_category_id":category, "candidate_limit":100})
        assert status == 200 and baseline["scope"]["evaluated_count"] <= 100
        codes = {c["ktru_code"] for c in baseline["candidates"] + baseline["excluded"]}
        assert len(codes) == baseline["scope"]["evaluated_count"]
        source_children = {code for code, row in records.items() if row["is_template"] is False
                           and (row.get("parent_position") or {}).get("code") == category}
        assert codes == source_children, "Selected scope differs from real source family"
        for code in sorted(codes):
            if code not in cards:
                status, card = fetch("/api/v1/catalog/positions/" + code)
                assert status == 200 and json.loads(card["position"]["raw_json"]) == records[code]
                cards[code] = card
        body = {"description":description, "confirmed_category_id":category, "candidate_limit":100}
        status, result = fetch("/api/v1/analyses", body)
        assert status == 200
        proofs = []
        for code in sorted(codes):
            source = {p["code"]:p for p in records[code]["characteristics"]}
            for prop in cards[code]["position"]["properties"]:
                raw = source[prop["id"]]
                if not prop["verified"] or prop["unsupported_reasons"] or raw["kind"] not in ("1", "2"):
                    continue
                values = raw["values"]
                if not values or not all("quality_description" in v for v in values):
                    continue
                allowed = {v["quality_description"].casefold() for v in values}
                for fact in result["facts"]:
                    if fact["unknown"] or fact["negated"] or fact["name"] != raw["name"]:
                        continue
                    if fact["value"].casefold() not in allowed:
                        proofs.append({"code":code, "property_id":prop["id"], "name":prop["name"],
                            "fact":fact, "source_values":values, "rule_source":prop["evidence"]})
        expected = {p["code"] for p in proofs}
        assert expected, "No independently proven contradiction: " + description
        remaining = {c["ktru_code"] for c in result["candidates"]}
        excluded = {c["ktru_code"] for c in result["excluded"]}
        resurrected = sorted(expected & remaining)
        if PHASE == "after":
            assert expected <= excluded and not resurrected
            assert all(not any(check["outcome"] == "incompatible" for check in c["checks"]) for c in result["candidates"])
            for c in result["excluded"]:
                assert c["outcome"] == "incompatible" and c["exclusion_reasons"]
            assert all(c["outcome"] == "unknown" for c in result["candidates"] if c["blocking_reasons"])
            assert result["selected_ktru_code"] is None
        manual_status, manual = fetch("/api/v1/analyses", {**body, "confirmed_ktru_code":sorted(expected)[0]})
        if PHASE == "after":
            assert manual_status == 422 and manual["detail"]["code"] == "incompatible_ktru_code"
        report["cases"].append({"description":description, "category":category, "source_proofs":proofs,
            "expected_excluded":sorted(expected), "resurrected":resurrected,
            "remaining":sorted(remaining), "excluded":sorted(excluded), "status":result["status"],
            "manual_status":manual_status})
        print(description + ": " + str(len(remaining)) + " remain / " + str(len(excluded)) +
              " excluded; source contradictions=" + str(len(expected)) + "; resurrected=" + str(len(resurrected)))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / (PHASE + ".json")).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    run()
