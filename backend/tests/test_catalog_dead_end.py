"""HTTP regressions for catalog defects that the user cannot repair through the form."""
import pytest
from fastapi.testclient import TestClient
from conftest import runtime


@pytest.mark.parametrize("defect", ["no_rules", "source_issue", "resolve_blocked", "unsupported"])
def test_catalog_defect_without_question_requires_data(catalog_data, monkeypatch, defect):
    import app.main as main
    table = next(p for p in catalog_data.positions if p.code == "TEST-TABLE")
    changes = {"properties": ()} if defect == "no_rules" else (
        {"source_issues": ("source defect",)} if defect == "source_issue" else
        {"resolve_blocked": True} if defect == "resolve_blocked" else
        {"properties": tuple(p.model_copy(update={"verified": False, "unsupported_reasons": ("unknown_kind",)})
                             for p in table.properties)})
    table = table.model_copy(update=changes)
    catalog = catalog_data.model_copy(update={"positions": tuple(table if p.code == table.code else p
                                                                  for p in catalog_data.positions)})
    monkeypatch.setattr(main, "resources", lambda: runtime(catalog))
    with TestClient(main.app) as client:
        body = {"description": "Стол; Высота: 750 мм", "confirmed_category_id": "furniture",
                "confirmed_ktru_code": table.code}
        response = client.post("/api/v1/analyses", json=body)
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "needs_data" and result["next_question"] is None
        assert result["selected_ktru_code"] is None and not result["verification"]["value_verified"]
        assert any(issue["code"] == "catalog_constraints_unavailable" for issue in result["issues"])
        assert result["next_steps"]
