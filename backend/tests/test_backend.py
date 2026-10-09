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
from app.models import Attribute, Category, CategoryAttribute, STE


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
        ([{"name": "Материал", "value": "бумага"}, {"name": "Объем", "value": "200", "unit": "мл"}], "resolved"),
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
        "title": "test", "attributes": [{"name": "Материал", "value": "картон"}]
    }).json()
    assert response["status"] == "resolved"


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
    response = backend[0].post("/api/v1/analyses", json={"title": "test", "attributes": attributes}).json()
    assert response["status"] == "needs_input"
    assert response["result"]["selected_ktru_code"] is None
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
    response = backend[0].post("/api/v1/analyses", json={"title": "test", "attributes": [
        {"name": "Материал", "value": "бумага"}, {"name": "Объем", "value": "200.00"}
    ]}).json()
    assert response["status"] == "resolved"


def test_ready_rejects_unmigrated_database(backend):
    engine = create_engine(f"sqlite:///{(backend[2] / 'empty.db').as_posix()}")

    def sessions():
        with sessionmaker(engine)() as session:
            yield session

    app.dependency_overrides[get_session] = sessions
    assert backend[0].get("/api/health/ready").status_code == 503
    engine.dispose()
