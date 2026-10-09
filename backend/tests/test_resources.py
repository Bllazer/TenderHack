import json
from pathlib import Path

import numpy as np
import pytest

from app.catalog import Catalog, digest, load_snapshot
from app.search import ANALYZER_VERSION, Analyzer, Search
from conftest import FixtureEncoder, runtime


def write_bundle(directory, catalog):
    raw = catalog.model_dump(mode="json")
    raw["synthetic"] = False
    catalog = Catalog.model_validate(raw)
    (directory / "catalog.json").write_text(catalog.model_dump_json(), encoding="utf-8")
    vectors = FixtureEncoder().encode([category.search_text for category in catalog.categories], "passage: ")
    np.save(directory / "vectors.npy", vectors, allow_pickle=False)
    (directory / "tokens.json").write_text(json.dumps([Analyzer().tokens(c.search_text) for c in catalog.categories]), encoding="utf-8")
    manifest = {"catalog_version": catalog.catalog_version, "analyzer_version": ANALYZER_VERSION,
                "model_id": "intfloat/multilingual-e5-small", "model_revision": "a" * 40,
                "sha256": {name: digest(directory / name) for name in ("catalog.json", "vectors.npy", "tokens.json")}}
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def test_bundle_checksums_and_alignment(catalog_data, tmp_path):
    write_bundle(tmp_path, catalog_data)
    first = load_snapshot(tmp_path)
    second = load_snapshot(tmp_path)
    assert first.catalog == second.catalog
    assert not first.vectors.flags.writeable
    (tmp_path / "tokens.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        load_snapshot(tmp_path)
    # An already loaded snapshot keeps the previous version, not a partial update.
    assert len(first.tokens) == 3


def test_bad_index_version_is_rejected(catalog_data, tmp_path):
    manifest = write_bundle(tmp_path, catalog_data)
    manifest["catalog_version"] = "other"
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        load_snapshot(tmp_path)


def test_hybrid_search_uses_raw_text_when_extraction_fails(catalog_data):
    _, search = runtime(catalog_data)
    assert search.find("Мне нужен письменный стол для офиса")[0].id == "furniture"
    assert search.find("Зимняя рабочая куртка")[0].id == "clothes"
    result = search.find("Электрическая дрель")
    assert result[0].id == "tools"
    assert set(result[0].reasons) == {"BM25", "E5"}
    assert result[0].rank_score == pytest.approx(2 / 61)


def test_readiness_empty_and_resources_loaded_once(monkeypatch):
    import app.main as main
    from fastapi.testclient import TestClient
    calls = []

    def empty():
        calls.append(True)
        return load_snapshot(Path("resources/catalog")), None

    monkeypatch.setattr(main, "resources", empty)
    with TestClient(main.app) as client:
        assert client.get("/api/health/live").status_code == 200
        assert client.get("/api/health/ready").status_code == 503
        for _ in range(2):
            assert client.post("/api/v1/analyses", json={"description": "Мощность: 650 Вт"}).json()["status"] == "needs_data"
    assert len(calls) == 1


def test_parallel_limit_and_internal_error_not_exposed(client, monkeypatch):
    import app.main as main
    for _ in range(2):
        main.app.state.slots.acquire()
    assert client.post("/api/v1/analyses", json={"description": "test"}).status_code == 429
    for _ in range(2):
        main.app.state.slots.release()

    def crash(*args):
        raise RuntimeError("INTERNAL SECRET EXAMPLE")

    monkeypatch.setattr(main, "analyze", crash)
    response = client.post("/api/v1/analyses", json={"description": "test"})
    assert response.status_code == 503
    assert "INTERNAL SECRET" not in response.text


def test_openapi_and_actual_response_share_contract(client):
    from app.schemas import AnalysisOut
    response = client.post("/api/v1/analyses", json={"description": "Стол письменный"})
    AnalysisOut.model_validate(response.json())
    spec = client.get("/api/openapi.json").json()
    assert set(spec["components"]["schemas"]["AnalysisOut"]["properties"]) == set(response.json())
    assert set(spec["paths"]["/api/v1/analyses"]["post"]["responses"]) >= {"200", "422"}
