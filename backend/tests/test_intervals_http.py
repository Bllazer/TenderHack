import json

import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize("value", ["(750;750)", "[750;750)", "(750;750]"])
@pytest.mark.parametrize("source", ["attributes", "description"])
def test_empty_interval_never_compatible_or_resolved(client, value, source):
    body = {"description": "Стол", "confirmed_category_id": "furniture"}
    if source == "attributes":
        body["attributes"] = [{"id": "height", "name": "Высота", "value": value, "unit": "мм"}]
    else:
        body["description"] = f"Стол; Высота: {value} мм"
    response = client.post("/api/v1/analyses", json=body)
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "needs_input" and result["selected_ktru_code"] is None
    assert not result["verification"]["value_verified"]
    assert result["candidate_count"] == 0
    assert result["excluded"][0]["outcome"] == "incompatible"
    assert result["excluded"][0]["checks"][0]["reason"] == "invalid_number"
    assert result["facts"][0]["normalized_value"] is None
    assert not any(item["kind"] == "auto_normalization" for item in result["corrections"])
    body["confirmed_ktru_code"] = "TEST-TABLE"
    assert client.post("/api/v1/analyses", json=body).status_code == 422


@pytest.mark.parametrize("value", ["[70; 80]", "(70; 80)", "[70; 80)", "(70; 80]", "[75; 75]"])
def test_bracket_interval_extraction_and_confirmation(client, value):
    text = f"Стол; Высота: {value} см"
    body = {"description": text, "confirmed_category_id": "furniture"}
    response = client.post("/api/v1/analyses", json=body)
    assert response.status_code == 200
    result = response.json()
    fact = result["facts"][0]
    assert fact["value"] == value.replace(" ", "") and fact["unit"] == "см"
    assert text[slice(*fact["span"])] == fact["raw"] == text[text.index(";") + 1:]
    assert result["candidates"][0]["outcome"] == "compatible"
    assert result["status"] == "needs_input" and result["selected_ktru_code"] is None
    body["confirmed_ktru_code"] = "TEST-TABLE"
    assert client.post("/api/v1/analyses", json=body).json()["status"] == "resolved"


@pytest.mark.parametrize("value", ["(−80; −70]", "[−80; −70)", "(−80; −70)"])
def test_negative_interval_preserves_sign_bounds_and_span(client, value):
    text = f"Стол; Высота: {value} см"
    result = client.post("/api/v1/analyses", json={"description": text, "confirmed_category_id": "furniture"}).json()
    fact = result["facts"][0]
    assert fact["value"] == value.replace(" ", "") and fact["unit"] == "см"
    assert text[slice(*fact["span"])] == fact["raw"]
    assert result["excluded"][0]["checks"][0]["reason"] == "out_of_range"
    assert result["selected_ktru_code"] is None


@pytest.mark.parametrize("left,right", [(False, False), (True, False), (False, True)])
def test_empty_catalog_interval_blocks_http_readiness(monkeypatch, catalog_data, tmp_path, left, right):
    import app.main as main
    raw = catalog_data.model_dump(mode="json")
    raw["synthetic"] = False
    rule = raw["positions"][3]["properties"][0]["allowed"][0]
    rule.update(lower="750", upper="750", lower_inclusive=left, upper_inclusive=right)
    (tmp_path / "catalog.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(main.settings, "catalog_dir", tmp_path)
    with TestClient(main.app) as api:
        ready = api.get("/api/health/ready")
        assert ready.status_code == 503 and ready.json()["detail"]["code"] == "resources_invalid"
        result = api.post("/api/v1/analyses", json={"description": "Стол; Высота: [75;75] см"}).json()
        assert result["status"] == "needs_data" and result["selected_ktru_code"] is None


def test_catalog_closed_point_is_allowed_via_http(monkeypatch, catalog_data):
    from app.catalog import Catalog
    from conftest import runtime
    import app.main as main
    raw = catalog_data.model_dump(mode="json")
    raw["positions"][3]["properties"][0]["allowed"][0].update(lower="750", upper="750")
    engine = runtime(Catalog.model_validate(raw))
    monkeypatch.setattr(main, "resources", lambda: engine)
    with TestClient(main.app) as api:
        result = api.post("/api/v1/analyses", json={"description": "Стол; Высота: [75;75] см",
                         "confirmed_category_id": "furniture", "confirmed_ktru_code": "TEST-TABLE"})
        assert result.status_code == 200 and result.json()["status"] == "resolved"
