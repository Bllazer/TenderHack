"""Live HTTP acceptance over selected clean5 records; no search/model mocks."""
import argparse
import gzip
import json
import urllib.request
import urllib.error
from pathlib import Path


def http(base, route, body=None):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    request = urllib.request.Request(base + route, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def run(base, selection, output, phase):
    rows = json.loads(selection.read_text(encoding="utf-8"))
    records = []
    for row in rows:
        category = (row.get("parent_position") or {}).get("code", row["code"])
        _, card = http(base, "/api/v1/catalog/positions/" + row["code"])
        records.append({"route": "GET /api/v1/catalog/positions/" + row["code"], "response": card})
        for prop in [p for p in row["characteristics"] if p["kind"] == "3" and p["type"] == "1"]:
            permitted = [v["quality_description"] for v in prop["values"]]
            scenarios = [("single", permitted[:1], False), ("multiple", permitted[:2], False),
                         ("duplicate", permitted[:1] * 2, False),
                         ("invalid", permitted[:1] + ["НЕВХОДЯЩИЙ В СПРАВОЧНИК ВАРИАНТ"], False),
                         ("unknown", permitted[:1], True), ("optional_absent", None, False)]
            for scenario, values, unknown in scenarios:
                attributes = [] if values is None else [{"id": "selected", "name": prop["name"], "values": values, "unknown": unknown}]
                # The old API cannot receive arrays; preserve the old request and
                # its response too, so transport changes don't hide the baseline.
                if phase == "before" and values is not None:
                    attributes = [{"id": "selected", "name": prop["name"], "value": ", ".join(values), "unknown": unknown}]
                body = {"description": row["name"], "confirmed_category_id": category, "attributes": attributes,
                        "candidate_limit": 100}
                status, result = http(base, "/api/v1/analyses", body)
                record = {"family": category, "code": row["code"], "property": prop["code"], "scenario": scenario,
                          "request": body, "http_status": status, "response": result}
                records.append(record)
                if phase == "after":
                    assert status == 200, record
                    candidate = next(c for c in result["candidates"] + result["excluded"] if c["ktru_code"] == row["code"])
                    check = next(c for c in candidate["checks"] if c["property_id"] == prop["code"])
                    expected = "incompatible" if scenario == "invalid" else "unknown" if scenario in ("unknown", "optional_absent") else "compatible"
                    assert check["outcome"] == expected, (record["code"], prop["name"], scenario, check)
                    if scenario == "invalid":
                        assert candidate in result["excluded"], record
                        assert any(c["outcome"] == "incompatible" and c["value"] == values[-1] for c in check["elements"])
                        rejected, detail = http(base, "/api/v1/analyses", {**body, "confirmed_ktru_code": row["code"]})
                        records.append({"scenario": "confirm_excluded", "request": {**body, "confirmed_ktru_code": row["code"]},
                                        "http_status": rejected, "response": detail})
                        assert rejected == 422 and detail["detail"]["code"] == "incompatible_ktru_code"
                    if scenario == "optional_absent":
                        assert check["reason"] == "not_provided", check
                    if expected == "compatible" and result["next_question"]:
                        assert result["next_question"].get("property_name") != prop["name"], "Verified fact asked again"
                    if expected == "compatible" and result["next_question"] and result["next_question"]["kind"] == "code":
                        chosen_body = {**body, "confirmed_ktru_code":row["code"]}
                        selected_status, selected = http(base, "/api/v1/analyses", chosen_body)
                        records.append({"scenario":"confirm_compatible", "request":chosen_body,"http_status":selected_status,"response":selected})
                        assert selected_status == 200 and selected["status"] == "resolved" and selected["selected_ktru_code"] == row["code"]
                print(phase, row["code"], prop["name"], scenario, status, result.get("status"), flush=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"phase": phase, "records": records}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://127.0.0.1:8000")
    p.add_argument("--selection", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--phase", choices=["before", "after"], required=True)
    a = p.parse_args()
    run(a.base, a.selection, a.output, a.phase)
