"""Opt-in live HTTP/source checks; no synthetic catalog and no artifact rebuilding.

Run from backend: python tests/real_shovel_smoke.py [base URL] [clean supply dir].
"""
import gzip
import json
import sys
import urllib.request
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"
SUPPLY = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("../ktru_clean")
CATEGORY = "25.99.29.120-00004"
CODES = ["25.99.29.120-0000000" + str(i) for i in (1, 2, 3)]


def request(path, body=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as response:
        assert response.status == 200
        return json.load(response)


def run():
    report = {"readiness":request("/api/health/ready"), "cases":[], "cards":[]}
    for text in ["лопата совковая", "ловпата совкавая", "Лопата; Тип лопаты: Совкавая"]:
        body = {"description":text}
        first = request("/api/v1/analyses", body)
        assert first["next_question"]["kind"] == "category"
        assert any(c["id"] == CATEGORY for c in first["categories"])
        body["confirmed_category_id"] = CATEGORY
        result = request("/api/v1/analyses", body)
        case = {"description":text, "initial":first, "confirmed_category":result}
        if text == "лопата совковая":
            fact = next(f for f in result["facts"] if f["name"] == "Тип лопаты")
            assert fact["span"] == [7,15] and fact["raw"] == "совковая"
            assert result["status"] == "needs_data" and result["next_question"] is None
        else:
            q = result["next_question"]
            assert result["status"] == "needs_input" and q["kind"] == "correction"
            token = next(o["value"] for o in q["options"] if "Совковая" in o["label"])
            accepted = request("/api/v1/analyses", {**body, "answers":[{"question_id":q["id"], "value":token}]})
            rejected = request("/api/v1/analyses", {**body, "answers":[{"question_id":q["id"], "value":"reject"}]})
            assert accepted["status"] == rejected["status"] == "needs_data"
            assert accepted["next_question"] is rejected["next_question"] is None
            assert any(f["value"] == "Совковая" and f["name"] == "Тип лопаты" for f in accepted["facts"])
            assert any(f["value"].casefold() == "совкавая" and not f["mappings"] for f in rejected["facts"])
            case.update(accepted=accepted, rejected=rejected)
        assert result["selected_ktru_code"] is None
        print(text + ": " + result["status"] + "; facts=" + str([(f["name"],f["value"],f["span"]) for f in result["facts"]]))
        report["cases"].append(case)
    source = {}
    with gzip.open(SUPPLY / "ktru_actual_all.jsonl.gz", "rt", encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if row.get("code") in CODES:
                source[row["code"]] = row
    for code in CODES:
        card = request("/api/v1/catalog/positions/" + code)
        assert json.loads(card["position"]["raw_json"]) == source[code]
        assert card["position"]["source_issues"] == [] and card["position"]["resolve_blocked_reason"] == []
        assert any(b["kind"] == "3" and "multiple_choice_not_supported" in b["unsupported_reasons"] for b in card["blocking_reasons"])
        if code == CODES[2]:
            assert sum("missing_source_unit" in b["unsupported_reasons"] for b in card["blocking_reasons"]) == 2
        report["cards"].append(card)
        print(code + ": " + "; ".join(b["explanation"] for b in card["blocking_reasons"]))
    Path(".tools/real-shovel-http.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    run()
