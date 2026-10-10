"""Unknown elsewhere cannot undo a proven contextual contradiction (HTTP + C bridge)."""
import pytest
from fastapi.testclient import TestClient

from app.catalog import Property
from conftest import runtime


@pytest.mark.parametrize("blocker", ["unsupported", "source_issue", "resolve_blocked"])
def test_proven_contradiction_excluded_despite_other_blocker(catalog_data, monkeypatch, blocker):
    import app.main as main
    def blocked(position):
        if not position.code.startswith("TEST-DRILL-") or position.is_template:
            return position
        if blocker == "unsupported":
            extra = Property(id="other", name="Особенности", value_type="unsupported", required=True,
                verified=False, kind="3", evidence="fixture", unsupported_reasons=("multiple_choice_not_supported",))
            return position.model_copy(update={"properties":position.properties + (extra,)})
        return position.model_copy(update={"source_issues":("fixture issue",)} if blocker == "source_issue"
                                   else {"resolve_blocked":True, "resolve_blocked_reason":("fixture block",)})
    catalog = catalog_data.model_copy(update={"positions":tuple(blocked(p) for p in catalog_data.positions)})
    monkeypatch.setattr(main, "resources", lambda: runtime(catalog))
    with TestClient(main.app) as client:
        body = {"description":"Дрель; Питание: от сети; Мощность: 650 Вт", "confirmed_category_id":"tools"}
        response = client.post("/api/v1/analyses", json=body)
        assert response.status_code == 200
        result = response.json()
        assert result["candidate_count"] == result["excluded_count"] == 1
        remaining, excluded = result["candidates"][0], result["excluded"][0]
        assert remaining["ktru_code"] == "TEST-DRILL-MAINS" and remaining["outcome"] == "unknown"
        assert remaining["blocking_reasons"] and not remaining["exclusion_reasons"]
        assert excluded["ktru_code"] == "TEST-DRILL-BATTERY" and excluded["outcome"] == "incompatible"
        failed = [c for c in excluded["checks"] if c["outcome"] == "incompatible"]
        assert failed and excluded["blocking_reasons"]
        assert all(i["property_id"] in {c["property_id"] for c in failed} and i["rule_source"]
                   for i in excluded["exclusion_reasons"])
        assert result["status"] == "needs_data" and result["selected_ktru_code"] is None
        forbidden = client.post("/api/v1/analyses", json={**body,"confirmed_ktru_code":excluded["ktru_code"]})
        assert forbidden.status_code == 422 and forbidden.json()["detail"]["code"] == "incompatible_ktru_code"
        chosen = client.post("/api/v1/analyses", json={**body,"confirmed_ktru_code":remaining["ktru_code"]}).json()
        assert chosen["status"] != "resolved" and chosen["selected_ktru_code"] is None


@pytest.mark.parametrize("untrusted", [{"verified":False}, {"unsupported_reasons":("untrusted_source",)}, {"evidence":""}])
def test_untrusted_contradicting_rule_does_not_prove_exclusion(catalog_data, monkeypatch, untrusted):
    import app.main as main
    battery = next(p for p in catalog_data.positions if p.code == "TEST-DRILL-BATTERY")
    changed = battery.model_copy(update={"properties":tuple(
        prop.model_copy(update=untrusted) if prop.name == "Питание" else prop for prop in battery.properties)})
    catalog = catalog_data.model_copy(update={"positions":tuple(changed if p.code == changed.code else p for p in catalog_data.positions)})
    monkeypatch.setattr(main, "resources", lambda: runtime(catalog))
    with TestClient(main.app) as client:
        result = client.post("/api/v1/analyses", json={"description":"Дрель; Питание: от сети; Мощность: 650 Вт",
                                                     "confirmed_category_id":"tools"}).json()
        candidate = next(c for c in result["candidates"] if c["ktru_code"] == changed.code)
        assert candidate["outcome"] == "unknown" and not candidate["exclusion_reasons"]
        assert not any(c["outcome"] == "incompatible" for c in candidate["checks"])
        assert result["status"] != "resolved" and result["selected_ktru_code"] is None
