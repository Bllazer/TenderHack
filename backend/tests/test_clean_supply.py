import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.catalog import Catalog, snapshot
from app.clean_supply import adapt_position, read_auxiliary, load_clean_supply
from app.checks import check_fact, evaluate
from app.components import Components, existing_components
from app.schemas import Fact


@pytest.fixture(scope="module")
def sample():
    return json.loads((Path(__file__).parent / "fixtures/clean5-real.json").read_text(encoding="utf-8"))


@pytest.fixture
def real_data(sample, tmp_path):
    for name, content in sample["auxiliary"].items():
        (tmp_path / name).write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    auxiliary, units, aliases, _ = read_auxiliary(tmp_path)
    positions = tuple(adapt_position(row, f"sha256:{sample['source_sha256']}#{row['code']}") for row in sample["positions"])
    return snapshot(Catalog(catalog_version="real-clean5-samples", source_description=sample["source"], validation_report="source fixtures",
        positions=positions, units=units, unit_aliases=aliases, auxiliary_json=json.dumps(auxiliary, ensure_ascii=False)),
        problem="search_index_missing", missing_resources=("category_mapping", "search_index"))


def test_preservation_parent_versions_and_values(real_data, sample):
    for row in sample["positions"]:
        pos = real_data.positions[row["code"]]
        assert json.loads(pos.raw_json) == row
        assert pos.version == str(row["version"]) and pos.source_status == row["status"]
        assert pos.parent_code == (row.get("parent_position") or {}).get("code")
        assert pos.no_new_features is row["no_new_features"]
        assert pos.no_new_features_evidence == row["no_new_features_reason"]
        assert pos.resolve_blocked is row["resolve_blocked"]
        for prop, original in zip(pos.properties, row["characteristics"]):
            assert prop.kind == original["kind"] and prop.choice_type == original["choice_type"]
            assert prop.required == original["is_required"] and prop.source_type == original["type"]
            assert json.loads(prop.raw_json) == original
            assert all(value.source.startswith(pos.source_path) for value in prop.allowed)
    assert real_data.positions[sample["cases"]["null"]].no_new_features is None
    assert real_data.children["10.83.13.120-00000001"]
    assert not real_data.categories  # The supply's heuristic labels are not a taxonomy.
    with pytest.raises(TypeError): real_data.positions["anything"] = None


def test_multiple_units_or_and_source_constraints_kept(real_data, sample):
    pos = real_data.positions[sample["cases"]["multiunit"]]
    prop = next(p for p in pos.properties if len({v.okei_code for v in p.allowed}) > 1)
    original = json.loads(prop.raw_json)
    assert {v.okei_code for v in prop.allowed} == {(v.get("okei") or {}).get("code") for v in original["values"]}
    assert {v.value_code for v in prop.allowed} == {v.get("value_code") for v in original["values"]}
    assert all(v.raw_unit for v in prop.allowed)
    assert any(not v.lower_inclusive or not v.upper_inclusive for p in real_data.positions.values() for q in p.properties for v in q.allowed if v.kind == "interval")


def test_unknown_source_unit_is_not_inferred_by_name(real_data):
    from app.clean_supply import OKEI_SUPPORTED
    unsupported = [prop for position in real_data.positions.values() for prop in position.properties
                   if prop.source_type == "2" and any(value.okei_code and value.okei_code not in OKEI_SUPPORTED for value in prop.allowed)]
    assert unsupported
    for prop in unsupported:
        assert not prop.verified and "unsupported_source_unit" in prop.unsupported_reasons
        assert json.loads(prop.raw_json)["values"]


def test_context_aliases_and_unknown_units(real_data):
    from app.catalog import Property
    def rule(code, unit):
        return Property(id="test", name="Test", value_type="number", required=True, verified=True, evidence="test",
                        allowed=[{"kind":"number", "value":"20", "unit":unit, "okei_code":code, "source":"test"}])
    for raw, code, unit in (("мин", "355", "мин"), ("с", "354", "с"), ("мин-1", "331", "об/мин")):
        fact = Fact(id="test", name="Test", value="20", unit=raw, raw=raw, source="input")
        assert check_fact(rule(code, unit), fact, real_data.resolve_unit)[0] == "compatible"
        assert real_data.resolve_unit(rule("2356", "Минута угла"), raw) is None
    assert real_data.resolve_unit(rule("003", "мм"), "мкм") is None
    assert real_data.resolve_unit(rule("003", "мм"), "неизвестное") is None
    for raw in ("штук", "штуки", "единица"):
        assert real_data.resolve_unit(rule("796", "шт"), raw) is not None
    assert len(real_data.catalog.units) == 126 and len(real_data.catalog.unit_aliases) == 63
    assert sum(len(u.spellings) for u in real_data.catalog.units) == 130


def test_kind_three_is_unknown_without_false_conflict(real_data, sample):
    position = real_data.positions[sample["cases"]["kind3"]]
    prop = next(p for p in position.properties if p.kind == "3")
    facts = [Fact(id=str(i), name=prop.name, value=value.value or "20", unit=value.unit, raw="explicit selection", source="input")
             for i, value in enumerate(prop.allowed[:2])]
    candidate, issues, _ = evaluate(position, facts, real_data.resolve_unit)
    assert candidate.outcome != "compatible"
    assert not any(issue.code == "conflicting_facts" and issue.property_id == prop.id for issue in issues)
    assert "multiple_choice_not_supported" in prop.unsupported_reasons


def test_actual_catalog_http_missing_index_and_blocked_codes(real_data, sample, monkeypatch):
    import app.main as main
    calls = []
    def loader():
        calls.append(True)
        return real_data, None
    monkeypatch.setattr(main, "resources", loader)
    with TestClient(main.app) as api:
        ready = api.get("/api/health/ready")
        assert ready.status_code == 503 and ready.json()["detail"]["code"] == "search_index_missing"
        assert ready.json()["detail"]["missing_resources"] == ["category_mapping", "search_index"]
        assert api.get("/api/v1/catalog/status").json()["catalog_loaded"]
        for pos in real_data.positions.values():
            result = api.post("/api/v1/analyses", json={"description":pos.name, "confirmed_ktru_code":pos.code})
            if pos.is_template:
                assert result.status_code == 422
            else:
                result = result.json()
                assert result["status"] == "needs_data" and result["selected_ktru_code"] is None
                assert not result["verification"]["value_verified"]
                assert result["verification"]["blocking_issues"]
                if pos.source_issues: assert any(i["code"] == "source_issue" for i in result["issues"])
        assert len(calls) == 1
    affected = [p for p in real_data.positions.values() if p.source_issues]
    assert len(affected) == 7
    for pos in affected:
        assert evaluate(pos, [], real_data.resolve_unit)[0].outcome == "unknown"
    no_rules = real_data.positions[sample["cases"]["no_rules"]]
    assert evaluate(no_rules, [], real_data.resolve_unit)[0].outcome == "unknown"


@pytest.mark.parametrize("missing", ["extractor", "checker", "questions"])
def test_component_absence_reports_needs_data(client, missing):
    import app.main as main
    current = existing_components()
    main.app.state.components = Components(**{key: None if key == missing else getattr(current, key)
                                              for key in ("extractor", "checker", "questions")})
    response = client.post("/api/v1/analyses", json={"description":"Стол; Высота: 750 мм"})
    assert response.status_code == 200 and response.json()["status"] == "needs_data"
    assert response.json()["issues"][0]["code"] == missing + "_missing"
    assert client.get("/api/health/ready").status_code == 503


def test_search_port_receives_current_facts(client):
    import app.main as main
    legacy = main.app.state.search
    calls = []
    class StateSearch:
        def find_state(self, state):
            calls.append(state)
            return legacy.find(state.description)
    main.app.state.search = StateSearch()
    response = client.post("/api/v1/analyses", json={"description":"Стол", "attributes":[
        {"id":"height", "name":"Высота", "value":"750", "unit":"мм"}]})
    assert response.status_code == 200
    assert calls[0].description == "Стол" and calls[0].facts[0].value == "750"


@pytest.mark.parametrize("matching", [True, False])
def test_prepared_snapshot_must_match_supply_version(monkeypatch, tmp_path, engine, matching):
    import app.main as main
    import app.clean_supply as supply
    (tmp_path / "manifest.json").write_text("{}")
    monkeypatch.setattr(main.settings, "catalog_dir", tmp_path)
    monkeypatch.setattr(main.settings, "clean_supply_dir", tmp_path)
    monkeypatch.setattr(main, "load_snapshot", lambda path: engine[0])
    monkeypatch.setattr(supply, "expected_supply_version", lambda path: engine[0].catalog.catalog_version if matching else "stale")
    monkeypatch.setattr(main, "E5Encoder", lambda *args: engine[1].encoder)
    data, search = main.resources()
    assert data.problem == (None if matching else "resources_invalid")
    assert bool(search) is matching


@pytest.mark.skipif(os.environ.get("RUN_CLEAN_SUPPLY_TESTS") != "1", reason="Full local clean (5) required")
def test_full_actual_supply_http(monkeypatch):
    import app.main as main
    data = load_clean_supply(main.settings.clean_supply_dir)
    monkeypatch.setattr(main, "resources", lambda: (data, None))
    assert len(data.positions) == 60087
    assert sum(p.no_new_features is None for p in data.positions.values()) == 3545
    with TestClient(main.app) as api:
        status = api.get("/api/v1/catalog/status").json()
        assert status["templates"] == 5897 and status["final_positions"] == 54190
        assert api.get("/api/health/ready").status_code == 503
        response = api.post("/api/v1/analyses", json={"description":"Чай черный (ферментированный)",
            "attributes":[{"id":"sort", "name":"Вид чая", "value":"Листовой"}], "confirmed_ktru_code":"10.83.13.120-00000003"})
        assert response.status_code == 200 and response.json()["status"] == "needs_data"
