import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app import import_catalog
from app.config import settings
from app.db import get_session
from app.main import app
from app.models import Analysis, Attribute, Category, CategoryAttribute, STE


@pytest.fixture
def backend(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    admin = None
    schema = "test_" + uuid4().hex
    if postgres_url := os.environ.get("TEST_DATABASE_URL"):
        admin = create_engine(postgres_url)
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        url = make_url(postgres_url).update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False)
    monkeypatch.setattr(settings, "database_url", url)
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    command.check(config)
    engine = create_engine(url, connect_args={"check_same_thread": False} if admin is None else {})
    factory = sessionmaker(engine)
    monkeypatch.setattr(import_catalog, "SessionLocal", factory)

    def sessions():
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = sessions
    with TestClient(app) as client:
        yield client, factory, tmp_path
    app.dependency_overrides.clear()
    engine.dispose()
    if admin is not None:
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def demo():
    return json.loads(Path("demo-catalog.json").read_text(encoding="utf-8"))


def load(backend, data=None):
    path = backend[2] / "catalog.json"
    path.write_text(json.dumps(data if data is not None else demo()), encoding="utf-8")
    import_catalog.load(path)


def snapshot(factory):
    with factory() as session:
        return {
            model.__tablename__: sorted(
                [tuple(str(getattr(row, column.name)) for column in model.__table__.columns)
                 for row in session.scalars(select(model))]
            ) for model in (Category, Attribute, CategoryAttribute, STE)
        }


def test_empty_then_demo_and_saved_results(backend):
    client, factory, _ = backend
    assert client.get("/api/health/ready").json() == {"status": "ok"}
    assert client.get("/api/v1/catalog/status").json() == {"categories": 0, "attributes": 0, "ste": 0}
    assert client.post("/api/v1/analyses", json={"title": "test"}).json()["status"] == "needs_data"
    load(backend)
    original = snapshot(factory)
    load(backend)
    assert snapshot(factory) == original
    assert client.get("/api/v1/catalog/status").json() == {"categories": 2, "attributes": 2, "ste": 1}
    for attributes, expected in [
        ([], "ambiguous"),
        ([{"name": "Материал", "value": "бумага"}], "needs_input"),
        ([{"name": "Материал", "value": "бумага"}, {"name": "Объем", "value": "200", "unit": "мл"}], "needs_input"),
    ]:
        response = client.post("/api/v1/analyses", json={"title": "test", "attributes": attributes})
        assert response.status_code == 201
        result = response.json()
        assert result["status"] == expected
        assert bool(result["result"]["selected_ktru_code"]) == (expected == "resolved")
        assert client.get(f"/api/v1/analyses/{result['id']}").json() == result


@pytest.mark.parametrize("bad_reference", ["parent", "attribute", "ste"])
def test_invalid_reference_rolls_back_entire_import(backend, bad_reference):
    load(backend)
    original = snapshot(backend[1])
    data = demo()
    data["attributes"][0]["unit"] = "л"
    data["categories"][0]["name"] = "changed"
    if bad_reference == "parent":
        data["categories"][0]["parent_code"] = "MISSING"
    elif bad_reference == "attribute":
        data["categories"][1]["attributes"].append({"attribute": "MISSING"})
    else:
        data["ste"][0]["ktru_code"] = "MISSING"
    with pytest.raises(ValueError):
        load(backend, data)
    assert snapshot(backend[1]) == original


def test_reimport_replaces_rules_and_updates_rows(backend):
    load(backend)
    data = demo()
    data["categories"][0]["attributes"] = []
    data["categories"][1]["attributes"][0]["required"] = False
    data["categories"][1]["attributes"][1]["allowed_values"] = ["картон"]
    data["ste"][0]["title"] = "changed"
    load(backend, data)
    with backend[1]() as session:
        assert len(session.scalars(select(CategoryAttribute)).all()) == 2
        assert session.scalar(select(STE)).title == "changed"
    response = backend[0].post("/api/v1/analyses", json={
        "title": "test", "attributes": [{"name": "Материал", "value": "картон"}],
        "confirmed_ktru_code": "DEMO-CUP-PAPER"
    }).json()
    assert response["status"] == "resolved"


@pytest.mark.parametrize("mode", ["replace", "clear", "omit"])
def test_category_attributes_import_contract(backend, mode):
    load(backend)
    original = snapshot(backend[1])["category_attributes"]
    data = demo()
    category = data["categories"][0]
    category["name"] = "updated metadata"
    if mode == "replace":
        category["attributes"] = [
            {"attribute": "Материал", "required": False, "allowed_values": ["стекло"]}
        ]
    elif mode == "clear":
        category["attributes"] = []
    else:
        del category["attributes"]
    load(backend, data)
    # Repeating each form must preserve its meaning and row identities.
    first = snapshot(backend[1])
    load(backend, data)
    assert snapshot(backend[1]) == first
    with backend[1]() as session:
        item = session.scalar(select(Category).where(Category.ktru_code == category["ktru_code"]))
        assert item.name == "updated metadata"
        rules = session.scalars(select(CategoryAttribute).where(CategoryAttribute.category_id == item.id)).all()
        if mode == "replace":
            assert len(rules) == 1
            assert session.get(Attribute, rules[0].attribute_id).name == "Материал"
            assert rules[0].required is False
            assert rules[0].allowed_values == ["стекло"]
        elif mode == "clear":
            assert rules == []
        else:
            assert first["category_attributes"] == original
        other = session.scalar(select(Category).where(Category.ktru_code == data["categories"][1]["ktru_code"]))
        assert len(session.scalars(select(CategoryAttribute).where(CategoryAttribute.category_id == other.id)).all()) == 2


@pytest.mark.parametrize("error", ["duplicate_rule", "alias", "cycle", "numeric"])
def test_invalid_catalog_rules_roll_back(backend, error):
    load(backend)
    original = snapshot(backend[1])
    data = demo()
    if error == "duplicate_rule":
        data["categories"][0]["attributes"].append(data["categories"][0]["attributes"][0])
    elif error == "alias":
        data["attributes"][1]["aliases"] = ["Объём"]
    elif error == "cycle":
        for category in data["categories"]:
            category["is_leaf"] = False
        data["categories"][0]["parent_code"] = data["categories"][1]["ktru_code"]
        data["categories"][1]["parent_code"] = data["categories"][0]["ktru_code"]
    else:
        data["categories"][0]["attributes"][0]["allowed_values"] = ["abc"]
    with pytest.raises(ValueError):
        load(backend, data)
    assert snapshot(backend[1]) == original


@pytest.mark.parametrize("extra,code", [
    ({"name": "Объем", "value": "200", "unit": "л"}, "unit_mismatch"),
    ({"name": "Объем", "value": "abc"}, "invalid_number"),
    ({"name": "MISSING", "value": "abc"}, "unknown_attribute"),
    ({"name": "Материал", "value": "пластик"}, "duplicate_attribute"),
])
def test_errors_never_resolve(backend, extra, code):
    load(backend)
    attributes = [{"name": "Материал", "value": "бумага"}]
    if extra["name"] != "Объем":
        attributes.append({"name": "Объем", "value": "200"})
    attributes.append(extra)
    response = backend[0].post("/api/v1/analyses", json={"title": "test", "attributes": attributes,
        "confirmed_ktru_code": "DEMO-CUP-PAPER"}).json()
    assert response["status"] == "needs_input"
    assert response["result"]["selected_ktru_code"] is None
    assert response["result"]["confirmed_by_user"] is False
    assert code in {issue["code"] for issue in response["result"]["issues"]}


def test_blank_required_value_and_dimensionless_unit(backend):
    load(backend)
    client = backend[0]
    assert client.post("/api/v1/analyses", json={"title": "test", "attributes": [
        {"name": "Материал", "value": "   "}
    ]}).status_code == 422
    result = client.post("/api/v1/analyses", json={"title": "test", "attributes": [
        {"name": "Материал", "value": "бумага", "unit": "кг"},
        {"name": "Объем", "value": "200", "unit": "мл"}
    ]}).json()
    assert result["status"] == "needs_input"
    assert result["result"]["issues"][0]["code"] == "unit_mismatch"


def test_numeric_allowed_values_use_numeric_equality(backend):
    data = demo()
    for category in data["categories"]:
        category["attributes"][0]["allowed_values"] = ["200,0"]
    load(backend, data)
    response = backend[0].post("/api/v1/analyses", json={"title": "test", "confirmed_ktru_code": "DEMO-CUP-PAPER", "attributes": [
        {"name": "Материал", "value": "бумага"}, {"name": "Объем", "value": "200.00"}
    ]}).json()
    assert response["status"] == "resolved"


def test_single_candidate_requires_manual_confirmation(backend):
    load(backend)
    body = {"title": "Стакан", "attributes": [
        {"name": "Материал", "value": "бумага"}, {"name": "Объем", "value": "200", "unit": "мл"}
    ]}
    client = backend[0]
    initial = client.post("/api/v1/analyses", json=body).json()
    assert initial["status"] == "needs_input"
    assert len(initial["result"]["candidates"]) == 1
    assert initial["result"]["selected_ktru_code"] is None
    assert initial["result"]["confirmed_by_user"] is False
    assert any("confirmed_ktru_code" in step for step in initial["result"]["next_steps"])
    confirmed = client.post("/api/v1/analyses", json={**body, "confirmed_ktru_code": "DEMO-CUP-PAPER"})
    assert confirmed.status_code == 201
    result = confirmed.json()
    assert result["status"] == "resolved"
    assert result["result"]["selected_ktru_code"] == "DEMO-CUP-PAPER"
    assert result["result"]["confirmed_by_user"] is True
    assert result["engine_version"] == "catalog-baseline-v2"
    assert result["id"] != initial["id"]
    assert client.get(f"/api/v1/analyses/{result['id']}").json() == result
    assert client.get(f"/api/v1/analyses/{initial['id']}").json() == initial


def test_manual_choice_between_compatible_candidates(backend):
    data = demo()
    for category in data["categories"]:
        category["attributes"][1]["allowed_values"] = ["бумага"]
    load(backend, data)
    body = {"title": "test", "attributes": [
        {"name": "Материал", "value": "бумага"}, {"name": "Объем", "value": "200"}
    ]}
    initial = backend[0].post("/api/v1/analyses", json=body).json()
    assert initial["status"] == "ambiguous"
    assert len(initial["result"]["candidates"]) == 2
    for code in ("DEMO-CUP-PAPER", "DEMO-CUP-PLASTIC"):
        result = backend[0].post("/api/v1/analyses", json={**body, "confirmed_ktru_code": code}).json()
        assert result["status"] == "resolved"
        assert result["result"]["selected_ktru_code"] == code
        assert result["result"]["confirmed_by_user"] is True


@pytest.mark.parametrize("code", ["DEMO-CUP-PLASTIC", "MISSING"])
def test_incompatible_confirmation_is_rejected_without_saving(backend, code):
    load(backend)
    with backend[1]() as session:
        before = session.scalars(select(Analysis)).all()
    response = backend[0].post("/api/v1/analyses", json={"title": "test", "confirmed_ktru_code": code,
        "attributes": [{"name": "Материал", "value": "бумага"}, {"name": "Объем", "value": "200"}]})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "incompatible_ktru_code"
    assert response.json()["detail"]["field"] == "confirmed_ktru_code"
    with backend[1]() as session:
        assert len(session.scalars(select(Analysis)).all()) == len(before)


def test_confirmation_cannot_bypass_required_attributes(backend):
    load(backend)
    result = backend[0].post("/api/v1/analyses", json={"title": "test", "confirmed_ktru_code": "DEMO-CUP-PAPER",
        "attributes": [{"name": "Материал", "value": "бумага"}]}).json()
    assert result["status"] == "needs_input"
    assert result["result"]["selected_ktru_code"] is None
    assert result["result"]["confirmed_by_user"] is False
    assert "Объем" in result["result"]["candidates"][0]["missing_required"]


@pytest.mark.parametrize("code", ["", "   "])
def test_blank_confirmation_is_invalid(backend, code):
    response = backend[0].post("/api/v1/analyses", json={"title": "test", "confirmed_ktru_code": code})
    assert response.status_code == 422


def test_confirmation_requires_nonempty_catalog(backend):
    response = backend[0].post("/api/v1/analyses", json={"title": "test", "confirmed_ktru_code": "MISSING"})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "incompatible_ktru_code"


def test_old_results_do_not_claim_manual_confirmation(backend):
    with backend[1]() as session:
        item = Analysis(title="old", input_attributes=[], status="resolved",
                        result={"selected_ktru_code": "DEMO-CUP-PAPER"}, engine_version="catalog-baseline-v1")
        session.add(item)
        session.commit()
        analysis_id = item.id
    response = backend[0].get(f"/api/v1/analyses/{analysis_id}").json()
    assert response["status"] == "resolved"
    assert response["result"]["confirmed_by_user"] is False
    assert response["engine_version"] == "catalog-baseline-v1"


def test_confirmation_checks_all_candidates_before_response_limit(backend):
    data = demo()
    template = data["categories"][1]
    data["categories"] = [{**template, "ktru_code": f"DEMO-{index:02d}"} for index in range(21)]
    load(backend, data)
    result = backend[0].post("/api/v1/analyses", json={"title": "test", "confirmed_ktru_code": "DEMO-20",
        "attributes": [{"name": "Материал", "value": "бумага"}, {"name": "Объем", "value": "200"}]}).json()
    assert result["status"] == "resolved"
    assert result["result"]["selected_ktru_code"] == "DEMO-20"
    assert len(result["result"]["candidates"]) == 20
    assert any(candidate["ktru_code"] == "DEMO-20" for candidate in result["result"]["candidates"])


def test_ready_rejects_unmigrated_database(backend):
    engine = create_engine(f"sqlite:///{(backend[2] / 'empty.db').as_posix()}")

    def sessions():
        with sessionmaker(engine)() as session:
            yield session

    app.dependency_overrides[get_session] = sessions
    assert backend[0].get("/api/health/ready").status_code == 503
    engine.dispose()
