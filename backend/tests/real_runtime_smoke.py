"""Explicit real-catalog HTTP checks against the running service; no fixture injection."""
import copy
import json
import os
from pathlib import Path

from docker_smoke import fetch

BASE = os.environ.get("SMOKE_FRONTEND", "http://127.0.0.1:8080")
CASES = [
    {"description": "Битум нефтяной строительный", "category": "19.20.42.124-00000003",
     "code": "19.20.42.124-00000002", "name": "Марка", "value": "БН 70/30"},
    {"description": "Глина огнеупорная", "category": "08.12.22.112-00000002",
     "code": "08.12.22.112-00000001", "name": "Марка глины огнеупорной", "value": "ПГА"},
    {"description": "Шпажки для шашлыка", "category": "16.29.12.000-00000095",
     "code": "16.29.12.000-00000094", "name": "Материал", "value": "Дерево"},
]


def post(body):
    status, result = fetch(BASE, "/api/v1/analyses", body)
    assert status == 200, (status, result)
    if result["status"] != "resolved":
        assert result["selected_ktru_code"] is None
    return result


def run():
    Path(".tools").mkdir(exist_ok=True)
    report = []
    for case in CASES:
        body = {"description": case["description"]}
        first = post(body)
        assert first["next_question"]["kind"] == "category"
        assert case["category"] in {x["id"] for x in first["categories"]}
        body["confirmed_category_id"] = case["category"]
        category = post(body)
        q = category["next_question"]
        if q and q["kind"] == "property":
            body["answers"] = [{"question_id": q["id"], "value": case["value"], "unknown": False}]
        else:
            body["attributes"] = [{"id": "trial", "name": case["name"], "value": case["value"]}]
        checked = post(body)
        assert checked["next_question"] and checked["next_question"]["kind"] == "code", checked
        body["confirmed_ktru_code"] = case["code"]
        final = post(body)
        assert final["status"] == "resolved" and final["selected_ktru_code"] == case["code"], final
        report.append({"case": case, "steps": [first, category, checked, final]})
        wrong = copy.deepcopy(body)
        wrong["confirmed_ktru_code"] = CASES[(CASES.index(case) + 1) % len(CASES)]["code"]
        assert fetch(BASE, "/api/v1/analyses", wrong)[0] == 422
        changed = {"description": case["description"], "confirmed_category_id": case["category"],
                   "attributes": [{"id": "changed", "name": case["name"], "value": "НЕСУЩЕСТВУЮЩЕЕ ЗНАЧЕНИЕ"}]}
        assert post(changed)["status"] != "resolved"
        unknown = {"description": case["description"], "confirmed_category_id": case["category"]}
        if q and q["kind"] == "property":
            unknown["answers"] = [{"question_id": q["id"], "unknown": True}]
            assert post(unknown)["status"] != "resolved"
        print(case["description"] + ": category -> values -> code -> resolved; incompatible/edit/unknown checked")
    typo = {"description": "Битум нефтяной строительный; Марка: БН 70/30; Маарка: БН 90/10",
            "confirmed_category_id": CASES[0]["category"], "confirmed_ktru_code": CASES[0]["code"]}
    proposed = post(typo)
    q = proposed["next_question"]
    assert proposed["status"] == "needs_input" and q["kind"] == "correction", proposed
    accepted = post({**typo, "answers": [{"question_id": q["id"], "value": "accept:Марка"}]})
    assert accepted["status"] != "resolved" and any(x["code"] == "conflicting_facts" for x in accepted["issues"])
    rejected = post({**typo, "answers": [{"question_id": q["id"], "value": "reject"}]})
    assert rejected["status"] == "resolved" and rejected["additional"]
    unknown = post({**typo, "answers": [{"question_id": q["id"], "unknown": True}]})
    assert unknown["status"] == "needs_input"
    report.append({"case": "real typo", "steps": [proposed, accepted, rejected, unknown]})
    print("Real typo: accept -> conflict; reject -> preserved additional; unknown -> needs_input")
    drill = {"description": "Дрель ручная электрическая; Тип питания: От сети; Мощность: 650 Вт; "
             "Минимальный диаметр зажима патрона: 0,5 мм; Максимальный диаметр зажима патрона: 13 мм; "
             "Максимальное количество оборотов: 1000 об/мин",
             "confirmed_category_id": "28.24.11.000-00000003"}
    for label, suffix in [("wrong unit", "650 кг"), ("missing unit", "650"), ("unknown unit", "650 кубитов")]:
        body = {**drill, "description": drill["description"].replace("650 Вт", suffix)}
        result = post(body)
        assert result["status"] != "resolved"
        report.append({"case": label, "steps": [result]})
        print(label + ": " + result["status"])
    report.append({"case": "drill", "steps": [post(drill)]})
    for label, description, category in [
        ("ambiguous tea", "Чай черный (ферментированный)", "10.83.13.120-00000001"),
        ("no rules", "Нефть сырая", "06.10.10.100-00000001"),
        ("unsupported kind", "Контур дыхательный, одноразового использования", "32.50.13.190-00009"),
    ]:
        result = post({"description": description, "confirmed_category_id": category})
        assert result["status"] != "resolved" and not result["verification"]["value_verified"]
        report.append({"case": label, "steps": [result]})
        print(label + ": " + result["status"] + "; question=" + str((result["next_question"] or {}).get("kind")))
        if label == "no rules":
            assert result["status"] == "needs_data" and any(x["code"] == "catalog_constraints_unavailable" for x in result["issues"])
    gas = {"description": "Газ горючий природный (газ естественный); Назначение: Промышленное и коммунально-бытовое; Объем баллона: 11 л",
           "confirmed_category_id": "06.20.10.110-00000009"}
    ambiguous = post(gas)
    assert ambiguous["status"] == "ambiguous" and ambiguous["candidate_count"] == 3
    assert all(x["outcome"] == "compatible" for x in ambiguous["candidates"])
    manual = post({**gas, "confirmed_ktru_code": "06.20.10.110-00000003"})
    assert manual["status"] == "ambiguous" and manual["selected_ktru_code"] is None
    report.append({"case": "real ambiguous gas", "steps": [ambiguous, manual]})
    print("Real gas: three compatible codes -> ambiguous; manual selection does not bypass uniqueness")
    Path(".tools/real-runtime-http.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    run()
