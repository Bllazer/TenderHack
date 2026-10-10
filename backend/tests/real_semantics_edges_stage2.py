"""Additional live acceptance: counts, units, requiredness and source defects."""
import argparse
import json
from pathlib import Path
from real_semantics_stage2 import http


def run(base, selection, output):
    rows = json.loads(selection.read_text(encoding="utf-8"))
    records = []

    def post(row, attributes, label, confirmed=None):
        body = {"description":row["name"], "confirmed_category_id":(row.get("parent_position") or {}).get("code", row["code"]),
                "attributes":attributes, "confirmed_ktru_code":confirmed,"candidate_limit":100}
        status, response = http(base, "/api/v1/analyses", body)
        assert status == 200, (label, status, response)
        records.append({"scenario":label,"request":body,"http_status":status,"response":response})
        print(label, response["status"], response["candidate_count"], flush=True)
        return response

    count_row = next(r for r in rows if any(p["name"] == "Количество бортов" for p in r["characteristics"]))
    count_prop = next(p for p in count_row["characteristics"] if p["name"] == "Количество бортов")
    scenarios = [("1",None,"compatible","allowed_range"),("3",None,"compatible","allowed_range"),
                 ("2",None,"incompatible","out_of_range"),("1","шт","compatible","allowed_range"),
                 ("1","см","incompatible","unit_mismatch"),("1","ед","incompatible","unit_mismatch"),
                 ("1.5",None,"incompatible","invalid_count"),("(1;1)",None,"incompatible","invalid_number")]
    for value, unit, outcome, reason in scenarios:
        result = post(count_row,[{"id":"count","name":count_prop["name"],"value":value,"unit":unit}],f"count {value} {unit}")
        candidate = next(c for c in result["candidates"] + result["excluded"] if c["ktru_code"] == count_row["code"])
        check = next(c for c in candidate["checks"] if c["property_id"] == count_prop["code"])
        assert (check["outcome"],check["reason"]) == (outcome,reason), check

    ribs = next(p for p in count_row["characteristics"] if p["name"] == "Количество ребер жесткости")
    for value, outcome in [("2","compatible"),("1","incompatible"),(">2","compatible")]:
        result = post(count_row,[{"id":"ribs","name":ribs["name"],"value":value}],f"ribs {value}")
        candidate = next(c for c in result["candidates"] + result["excluded"] if c["ktru_code"] == count_row["code"])
        assert next(c for c in candidate["checks"] if c["property_id"] == ribs["code"])["outcome"] == outcome

    # A numeric class without a reviewed interpretation must remain unknown.
    row = next(r for r in rows if not r["source_issues"] and any(p["type"] == "2" and
        not any(v.get("okei") for v in p["values"]) and p["code"] not in (count_prop["code"], ribs["code"])
        for p in r["characteristics"]))
    prop = next(p for p in row["characteristics"] if p["type"] == "2" and not any(v.get("okei") for v in p["values"]))
    result = post(row,[{"id":"unchecked","name":prop["name"],"value":"1"}],"unreviewed numeric designation",row["code"])
    candidate = next(c for c in result["candidates"] if c["ktru_code"] == row["code"])
    check = next(c for c in candidate["checks"] if c["property_id"] == prop["code"])
    assert check["outcome"] == "unknown" and result["selected_ktru_code"] is None
    assert any(b["property_id"] == prop["code"] and b["applicability"] == "provided" and b["blocks_resolution"] for b in candidate["blocking_reasons"])

    for row in [r for r in rows if r["source_issues"]]:
        if row["is_template"]:
            card_status, card = http(base,"/api/v1/catalog/positions/" + row["code"])
            assert card_status == 200 and any(b["applicability"]=="source" and b["blocks_resolution"] for b in card["blocking_reasons"])
            records.append({"scenario":"source defect template card","route":"GET /api/v1/catalog/positions/"+row["code"],"http_status":card_status,"response":card})
            body={"description":row["name"],"confirmed_category_id":row["code"],"confirmed_ktru_code":row["code"]}
            status,response=http(base,"/api/v1/analyses",body)
            records.append({"scenario":"source template confirmation rejected","request":body,"http_status":status,"response":response})
            assert status==422 and response["detail"]["code"] in ("incompatible_ktru_code","template_confirmation")
            continue
        result = post(row, [], "actual source defect " + row["code"],row["code"])
        candidate = next(c for c in result["candidates"] if c["ktru_code"] == row["code"])
        assert result["selected_ktru_code"] is None and result["status"] != "resolved"
        assert any(b["applicability"] == "source" and b["blocks_resolution"] for b in candidate["blocking_reasons"])

    # Source-defined mandatory fields, including an unknown answer.
    for row in [r for r in rows if r["code"] in (count_row["code"],)]:
        result = post(row,[],"mandatory omission")
        assert any(c["missing_required"] for c in result["candidates"])
        question = result["next_question"]
        assert question and question["kind"] == "property"
        body = {"description":row["name"],"confirmed_category_id":(row.get("parent_position") or {}).get("code",row["code"]),
                "answers":[{"question_id":question["id"],"unknown":True}],"candidate_limit":100}
        status, response = http(base,"/api/v1/analyses",body)
        records.append({"scenario":"unknown mandatory answer","request":body,"http_status":status,"response":response})
        assert status == 200 and response["selected_ktru_code"] is None and not response["verification"]["value_verified"]
    output.write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding="utf-8")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--base",default="http://127.0.0.1:8000")
    p.add_argument("--selection",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    a=p.parse_args()
    run(a.base,a.selection,a.output)
