"""HTTP regressions on synthetic rules, separate from the real supply smoke test."""
import pytest
from fastapi.testclient import TestClient

from app.catalog import AllowedValue, Catalog, Category, Position, Property
from conftest import runtime


@pytest.fixture
def context_client(monkeypatch):
    import app.main as main
    def prop(pid, name, values, **kwargs):
        return Property(id=pid, name=name, value_type="text", required=True, verified=True, evidence="fixture",
            allowed=tuple(AllowedValue(kind="enum", value=v, source="fixture") for v in values), **kwargs)
    positions = (
        Position(code="TEST-SCOOP", name="Лопата", is_template=False, status="actual", source_path="fixture",
                 properties=(prop("type", "Тип лопаты", ["Совковая"]),)),
        Position(code="TEST-BAYONET", name="Лопата", is_template=False, status="actual", source_path="fixture",
                 properties=(prop("type", "Тип лопаты", ["Штыковая"]),)),
        Position(code="TEST-COLOR", name="Стул", is_template=False, status="actual", source_path="fixture",
                 properties=(prop("color", "Цвет", ["Красный"]),)),
        Position(code="TEST-AMB", name="Деталь", is_template=False, status="actual", source_path="fixture",
                 properties=(prop("a", "Цвет корпуса", ["Красный"]), prop("b", "Цвет ручки", ["Красный"]))),
        Position(code="TEST-BLOCKED", name="Лопата", is_template=False, status="actual", source_path="fixture",
                 properties=(prop("type", "Тип лопаты", ["Совковая"]),
                    prop("features", "Конструктивные особенности", ["Усиленная"], kind="3",
                         unsupported_reasons=("multiple_choice_not_supported",)))),
    )
    categories = tuple(Category(id=cid, name=cid, search_text=cid, position_codes=codes,
        link_verified=True, coverage_verified=True, evidence="fixture") for cid, codes in
        [("shovels", ("TEST-SCOOP", "TEST-BAYONET")), ("chairs", ("TEST-COLOR",)),
         ("amb", ("TEST-AMB",)), ("blocked", ("TEST-BLOCKED",))])
    data = Catalog(catalog_version="fixture", source_description="fixture", validation_report="fixture",
                   categories=categories, positions=positions)
    monkeypatch.setattr(main, "resources", lambda: runtime(data))
    with TestClient(main.app) as client:
        yield client


def post(client, description, category="shovels", **state):
    response = client.post("/api/v1/analyses", json=dict(description=description, confirmed_category_id=category, **state))
    assert response.status_code == 200, response.text
    return response.json()


def decide(client, description, first, value, **state):
    return post(client, description, answers=[dict(question_id=first["next_question"]["id"], value=value)], **state)


def test_exact_value_after_category_confirmation_and_no_repeat(context_client):
    text = "лопата совковая"
    first = context_client.post("/api/v1/analyses", json={"description":text}).json()
    assert first["facts"] == []
    result = post(context_client, text)
    fact = result["facts"][0]
    assert (fact["name"], fact["value"], fact["raw"], fact["span"]) == ("Тип лопаты", "совковая", "совковая", [7,15])
    assert text[slice(*fact["span"])] == fact["raw"]
    assert result["next_question"]["kind"] == "code"
    confirmed = post(context_client, text, confirmed_ktru_code="TEST-SCOOP")
    assert confirmed["status"] == "resolved" and confirmed["next_question"] is None


@pytest.mark.parametrize("text", ["ловпата совкавая", "Лопата; Тип лопаты: Совкавая"])
def test_value_typo_requires_confirmation_and_full_recheck(context_client, text):
    first = post(context_client, text)
    assert first["status"] == "needs_input" and first["selected_ktru_code"] is None
    assert first["next_question"]["kind"] == "correction"
    token = next(o["value"] for o in first["next_question"]["options"] if "Совковая" in o["label"])
    accepted = decide(context_client, text, first, token)
    assert accepted["facts"][0]["value"] == "Совковая"
    assert accepted["facts"][0]["raw"] == first["facts"][0]["raw"]
    assert accepted["next_question"]["kind"] == "code"
    rejected = decide(context_client, text, first, "reject")
    assert rejected["selected_ktru_code"] is None
    assert rejected["facts"][0]["value"].casefold() == "совкавая" and not rejected["facts"][0]["mappings"]
    assert rejected["next_question"]["kind"] == "property"


def test_acceptance_detects_conflict_and_edit_replaces_value(context_client):
    text = "Лопата; Тип лопаты: Штыковая; Тип лопаты: Совкавая"
    first = post(context_client, text)
    token = first["next_question"]["options"][0]["value"]
    result = decide(context_client, text, first, token)
    assert result["next_question"]["kind"] == "conflict" and result["selected_ktru_code"] is None
    original = post(context_client, "лопата совковая")
    edited = post(context_client, "лопата совковая", attributes=[dict(id="edit", name="Тип лопаты", value="Штыковая",
                      replaces=original["facts"][0]["id"])])
    assert edited["candidates"][0]["ktru_code"] == "TEST-BAYONET"
    assert edited["next_question"]["kind"] == "code"


def test_other_category_and_ambiguous_assignment(context_client):
    assert post(context_client, "Стул красный", "chairs")["next_question"]["kind"] == "code"
    result = post(context_client, "Деталь красный", "amb")
    assert result["facts"][0]["name"] == "" and result["next_question"]["kind"] == "correction"
    assert len(result["next_question"]["options"]) == 3
    assert post(context_client, "лопата совковая", "chairs")["facts"] == []


@pytest.mark.parametrize("text", ["лопата не совковая", "лопата без совковой", "лопата совковаяish",
    "лопата не совсем совковая", "совковая не нужна", "не только совковая", "возможно совковая",
    "кажется совковая", "совковая?"])
def test_negation_and_partial_words_never_verify_positive(context_client, text):
    result = post(context_client, text)
    assert not result["verification"]["value_verified"] and result["selected_ktru_code"] is None
    assert not any(f["value"] == "совковая" and not f["unknown"] for f in result["facts"])


def test_uncertain_typo_cannot_silently_disappear_next_to_verified_value(context_client):
    result = post(context_client, "Лопата; Тип лопаты: Совковая; не совсем совкавая",
                  confirmed_ktru_code="TEST-SCOOP")
    assert result["status"] == "needs_input" and result["selected_ktru_code"] is None
    assert result["next_question"]["kind"] == "correction"
    assert all(not o["value"].startswith("accept_value:") for o in result["next_question"]["options"])


def test_blocked_rules_card_and_no_pointless_required_question(context_client):
    result = post(context_client, "лопата совковая", "blocked")
    assert result["status"] == "needs_data" and result["next_question"] is None
    reason = result["candidates"][0]["blocking_reasons"][0]
    assert (reason["property_id"], reason["kind"], reason["unsupported_reasons"]) == (
        "features", "3", ["multiple_choice_not_supported"])
    card = context_client.get("/api/v1/catalog/positions/TEST-BLOCKED")
    assert card.status_code == 200 and card.json()["blocking_reasons"] == [reason]
    assert card.json()["position"]["properties"][1]["allowed"][0]["value"] == "Усиленная"
    assert context_client.get("/api/v1/catalog/positions/missing").status_code == 404
