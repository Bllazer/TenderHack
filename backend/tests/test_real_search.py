import json
import os
from pathlib import Path

import numpy as np
import pytest

from app.catalog import load_snapshot
from app.analysis import analyze
from app.prepare import prepare
from app.schemas import AnalysisCreate
from app.search import E5Encoder, Search


@pytest.mark.skipif(os.environ.get("RUN_E5_TESTS") != "1", reason="Offline E5 weights and search extra required")
def test_real_local_e5_and_offline_index(catalog_data, tmp_path, monkeypatch):
    directory = Path(os.environ.get("TEST_MODEL_DIR", "resources/model"))
    metadata = json.loads((directory / "model.meta.json").read_text(encoding="utf-8"))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    # This temporary software fixture is never published into resources/catalog.
    raw = catalog_data.model_dump(mode="json")
    raw["synthetic"] = False
    (tmp_path / "catalog.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    prepare(tmp_path, directory)
    data = load_snapshot(tmp_path)
    encoder = E5Encoder(directory, metadata["revision"])
    query = encoder.encode(["Электрическая дрель от сети"], "query: ")
    assert query.shape == (1, 384)
    assert np.linalg.norm(query[0]) == pytest.approx(1, abs=1e-5)
    search = Search(data, encoder)
    assert search.find("Электрическая дрель от сети")[0].id == "tools"
    body = AnalysisCreate(description="Дрель электрическая; питание: от сети; мощность: 650 Вт")
    initial = analyze(data, search, body)
    assert initial.status == "needs_input" and initial.categories[0].id == "tools"
    unconfirmed = analyze(data, search, body.model_copy(update={"confirmed_category_id": "tools"}))
    assert unconfirmed.status == "needs_input" and unconfirmed.selected_ktru_code is None
    final = analyze(data, search, body.model_copy(update={"confirmed_category_id": "tools", "confirmed_ktru_code": "TEST-DRILL-MAINS"}))
    assert final.status == "resolved" and final.verification.value_verified
