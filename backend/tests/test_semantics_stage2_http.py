"""Isolated HTTP regressions; real-supply acceptance is real_semantics_stage2.py."""
import pytest
from fastapi.testclient import TestClient
from app.catalog import AllowedValue, Property
from conftest import runtime
from app.structured import structure
from app.schemas import Fact


def client_with(monkeypatch, catalog, rule, *, competing=False):
    import app.main as main
    positions = []
    for p in catalog.positions:
        if p.code == "TEST-DRILL-MAINS" or competing and p.code == "TEST-DRILL-BATTERY":
            p = p.model_copy(update={"properties": p.properties + (rule,)})
        positions.append(p)
    monkeypatch.setattr(main, "resources", lambda: runtime(catalog.model_copy(update={"positions":tuple(positions)})))
    return TestClient(main.app)


def rule(required=False, **kwargs):
    return Property(id="features", name="Особенности", value_type="text", required=required,
        kind="3", verified=True, evidence="synthetic regression only",
        allowed=tuple(AllowedValue(kind="enum", value=v, value_code=v, source="test") for v in ("Первый", "Второй")), **kwargs)


BASE = {"description":"Дрель; Питание: от сети; Мощность:650 Вт", "confirmed_category_id":"tools",
        "confirmed_ktru_code":"TEST-DRILL-MAINS"}


def test_array_is_not_empty_or_uncertain_in_c_contract():
    fact = Fact(id="f", name="Особенности", value="", values=["Первый","Второй"], raw='["Первый","Второй"]', source="input")
    result = structure(fact)
    assert result.kind == "text_set" and result.polarity == "affirmed" and result.extraction_confidence == 1


@pytest.mark.parametrize("values,expected", [(["Первый"], "compatible"), (["Первый","Второй"], "compatible"),
    (["Первый","Первый"], "compatible"), (["Первый","Недопустимый"], "incompatible")])
def test_selected_values_element_checks(monkeypatch, catalog_data, values, expected):
    with client_with(monkeypatch, catalog_data, rule()) as client:
        body = {**BASE, "confirmed_ktru_code":None, "attributes":[{"id":"features", "name":"Особенности", "values":values}]}
        response = client.post("/api/v1/analyses", json=body)
        assert response.status_code == 200
        result = response.json()
        candidate = next(c for c in result["candidates"] + result["excluded"] if c["ktru_code"] == BASE["confirmed_ktru_code"])
        check = next(c for c in candidate["checks"] if c["property_id"] == "features")
        assert check["outcome"] == expected
        assert [e["value"] for e in check["elements"]] == values
        assert result["facts"][-1]["values"] == values and result["facts"][-1]["raw"]
        confirmed = client.post("/api/v1/analyses", json={**body,"confirmed_ktru_code":BASE["confirmed_ktru_code"]})
        if expected == "incompatible":
            assert confirmed.status_code == 422
            assert check["elements"][-1]["reason"] == "invalid_value"
        else:
            assert confirmed.json()["status"] == "resolved"


def test_required_multiple_answer_and_unknown(monkeypatch, catalog_data):
    with client_with(monkeypatch, catalog_data, rule(required=True)) as client:
        initial = client.post("/api/v1/analyses", json=BASE).json()
        assert initial["status"] == "needs_input" and initial["selected_ktru_code"] is None
        q = initial["next_question"]
        assert q["multiple"] and q["property_name"] == "Особенности"
        answered = {**BASE,"answers":[{"question_id":q["id"],"values":["Первый","Второй"]}]}
        accepted = client.post("/api/v1/analyses", json=answered).json()
        assert accepted["status"] == "resolved" and accepted["next_question"] is None
        edited = {**answered,"attributes":[{"id":"old","name":"Особенности","values":["Первый"]}],
                  "answers":[{"question_id":q["id"],"values":["Второй"]}]}
        replacement = client.post("/api/v1/analyses",json=edited).json()
        assert replacement["status"] == "resolved"
        assert [f["values"] for f in replacement["facts"] if f["name"]=="Особенности"] == [["Второй"]]
        assert any(c["original"]=='["Первый"]' for c in replacement["corrections"])
        edited["attributes"][0]["replaces"] = "answer:" + q["id"]
        explicit_edit = client.post("/api/v1/analyses",json=edited).json()
        assert explicit_edit["status"] == "resolved"
        assert [f["values"] for f in explicit_edit["facts"] if f["name"]=="Особенности"] == [["Первый"]]
        answered["answers"][0]["unknown"] = True
        unknown = client.post("/api/v1/analyses", json=answered).json()
        assert unknown["status"] != "resolved" and unknown["selected_ktru_code"] is None


def test_optional_unsupported_applicability(monkeypatch, catalog_data):
    unsupported = rule().model_copy(update={"verified":False,"value_type":"unsupported", "unsupported_reasons":("unknown_semantics",)})
    with client_with(monkeypatch, catalog_data, unsupported) as client:
        absent = client.post("/api/v1/analyses", json=BASE).json()
        assert absent["status"] == "resolved"
        reason = absent["candidates"][0]["blocking_reasons"][0]
        assert reason["applicability"] == "potential" and not reason["blocks_resolution"]
        supplied = client.post("/api/v1/analyses", json={**BASE,"attributes":[{"id":"f", "name":"Особенности", "values":["Первый"]}]}).json()
        assert supplied["selected_ktru_code"] is None and supplied["status"] != "resolved"
        reason = supplied["candidates"][0]["blocking_reasons"][0]
        assert reason["applicability"] == "provided" and reason["blocks_resolution"]


def test_optional_source_defect_blocks_even_when_absent(monkeypatch, catalog_data):
    invalid = rule().model_copy(update={"verified":False,"unsupported_reasons":("invalid_source_constraint",)})
    with client_with(monkeypatch, catalog_data, invalid) as client:
        result = client.post("/api/v1/analyses", json=BASE).json()
        assert result["selected_ktru_code"] is None
        reason = result["candidates"][0]["blocking_reasons"][0]
        assert reason["applicability"] == "source" and reason["blocks_resolution"]


def test_unknown_competitor_does_not_prove_unique_code(monkeypatch, catalog_data):
    positions=[]
    for position in catalog_data.positions:
        if position.code == "TEST-DRILL-BATTERY":
            properties = tuple(p.model_copy(update={"required":False}) if p.name == "Напряжение" else
                p.model_copy(update={"allowed":(AllowedValue(kind="enum",value="от сети",source="fixture"),)})
                for p in position.properties)
            uncertain = Property(id="unchecked-power",name="Мощность",value_type="unsupported",verified=False,
                required=False,evidence="fixture",unsupported_reasons=("unsupported_semantics",))
            position=position.model_copy(update={"properties":properties+(uncertain,)})
        positions.append(position)
    changed=catalog_data.model_copy(update={"positions":tuple(positions)})
    with client_with(monkeypatch, changed, rule()) as client:
        result=client.post("/api/v1/analyses",json=BASE).json()
        assert result["candidate_count"] == 2 and not result["verification"]["ktru_unique"]
        assert result["selected_ktru_code"] is None and result["status"] != "resolved"
        other=next(c for c in result["candidates"] if c["ktru_code"]=="TEST-DRILL-BATTERY")
        assert other["outcome"]=="unknown" and any(b["applicability"]=="provided" for b in other["blocking_reasons"])


@pytest.mark.parametrize("values,outcome", [(["1","2"],"compatible"),(["1","3"],"incompatible")])
def test_numeric_multiple_values_reach_c(monkeypatch, catalog_data, values, outcome):
    numeric = rule().model_copy(update={"name":"Размеры", "value_type":"number", "source_type":"2",
        "allowed":tuple(AllowedValue(kind="number",value=v,unit="см",okei_code="004",source="fixture") for v in ("1","2"))})
    with client_with(monkeypatch, catalog_data, numeric) as client:
        body = {**BASE,"confirmed_ktru_code":None,"attributes":[{"id":"f","name":"Размеры","values":values,"unit":"см"}]}
        result=client.post("/api/v1/analyses",json=body).json()
        c=next(c for c in result["candidates"]+result["excluded"] if c["ktru_code"]==BASE["confirmed_ktru_code"])
        assert next(x for x in c["checks"] if x["property_id"]=="features")["outcome"]==outcome
        if outcome=="compatible":
            assert c["outcome"]=="compatible" and result["verification"]["value_verified"]


def test_no_okei_is_not_implicitly_dimensionless(monkeypatch, catalog_data):
    number = Property(id="class",name="Числовой класс",value_type="number",verified=True,required=False,
        evidence="fixture",allowed=(AllowedValue(kind="number",value="2",source="fixture"),))
    with client_with(monkeypatch, catalog_data, number) as client:
        result = client.post("/api/v1/analyses", json={**BASE,"attributes":[{"id":"n", "name":"Числовой класс", "value":"2"}]}).json()
        assert result["selected_ktru_code"] is None
        check = next(c for c in result["candidates"][0]["checks"] if c["property_id"] == "class")
        assert check["reason"] == "numeric_semantics_unknown"


def test_single_choice_rejects_two_distinct_values(monkeypatch, catalog_data):
    with client_with(monkeypatch, catalog_data, rule().model_copy(update={"kind":"2"})) as client:
        body = {**BASE,"confirmed_ktru_code":None,"attributes":[{"id":"f", "name":"Особенности", "values":["Первый","Второй"]}]}
        result = client.post("/api/v1/analyses", json=body).json()
        candidate = next(c for c in result["excluded"] if c["ktru_code"] == BASE["confirmed_ktru_code"])
        check = next(c for c in candidate["checks"] if c["property_id"] == "features")
        assert check["reason"] == "multiple_values_not_allowed"


def test_scalar_and_array_are_mutually_exclusive(client):
    response = client.post("/api/v1/analyses", json={**BASE,"attributes":[{"id":"f","name":"Особенности","value":"Первый","values":["Второй"]}]})
    assert response.status_code == 422
