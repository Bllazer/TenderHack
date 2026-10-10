"""Reusing a matrix must prove exact document ordering, not just equal counts."""
import json
import numpy as np
import pytest
from app.catalog import digest
from app.prepare import prepare
from app.search import ANALYZER_VERSION, MODEL_ID


def test_reuse_rejects_changed_search_document_and_accepts_constraint_refresh(tmp_path, catalog_data):
    old, new, model = (tmp_path / name for name in ("old","new","model"))
    for directory in (old,new,model):
        directory.mkdir()
    catalog = catalog_data.model_copy(update={"synthetic":False})
    (old / "catalog.json").write_text(catalog.model_dump_json(),encoding="utf-8")
    vectors = np.zeros((len(catalog.categories),384),dtype=np.float32)
    vectors[:,0] = 1
    np.save(old / "vectors.npy",vectors)
    (old / "tokens.json").write_text(json.dumps([["fixture"] for _ in catalog.categories]),encoding="utf-8")
    revision="a"*40
    manifest={"catalog_version":catalog.catalog_version,"analyzer_version":ANALYZER_VERSION,"model_id":MODEL_ID,
              "model_revision":revision,"sha256":{n:digest(old/n) for n in ("catalog.json","tokens.json","vectors.npy")}}
    (old / "manifest.json").write_text(json.dumps(manifest),encoding="utf-8")
    (model / "model.meta.json").write_text(json.dumps({"model_id":MODEL_ID,"revision":revision,"sha256":{}}),encoding="utf-8")
    changed=catalog.model_copy(update={"catalog_version":"updated", "categories":tuple(
        c.model_copy(update={"search_text":c.search_text+" changed"}) for c in catalog.categories)})
    (new / "catalog.json").write_text(changed.model_dump_json(),encoding="utf-8")
    with pytest.raises(ValueError,match="Search documents/order changed"):
        prepare(new,model,reuse_from=old)
    refreshed=catalog.model_copy(update={"catalog_version":"updated"})
    (new / "catalog.json").write_text(refreshed.model_dump_json(),encoding="utf-8")
    prepare(new,model,reuse_from=old)
    assert digest(new / "vectors.npy")==digest(old / "vectors.npy")
    assert json.loads((new / "manifest.json").read_text())["catalog_version"]=="updated"
