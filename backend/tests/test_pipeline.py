import json
from decimal import Decimal

import pytest

from app.analysis import InvalidConfirmation, analyze
from app.catalog import Catalog, Property, load_snapshot, snapshot
from app.checks import Interval, check_fact, number, subset
from app.extraction import extract
from app.schemas import AnalysisCreate, Fact
from app.search import canon
from app.units import get_unit
from conftest import runtime


def request(engine, **kwargs):
    return analyze(*engine, AnalysisCreate(description=kwargs.pop("description", "Дрель; питание: от сети; мощность: 650 Вт"), **kwargs))


def test_text_category_question_answer_confirmation(client):
    body = {"description": "Дрель электрическая; питание: от сети"}
    first = client.post("/api/v1/analyses", json=body)
    assert first.status_code == 200
    result = first.json()
    assert result["status"] == "needs_input"
    assert result["next_question"]["kind"] == "category"
    assert result["categories"][0]["id"] == "tools"
    body["confirmed_category_id"] = "tools"
    second = client.post("/api/v1/analyses", json=body).json()
    assert second["scope"]["position_count"] == 2
    assert second["candidate_count"] == 1
    assert second["next_question"]["property_name"] == "Мощность"
    body["answers"] = [{"question_id": second["next_question"]["id"], "value": "650", "unit": "Вт"}]
    third = client.post("/api/v1/analyses", json=body).json()
    assert third["status"] == "needs_input"
    assert third["next_question"]["kind"] == "code"
    assert third["verification"]["value_verified"]
    body["confirmed_ktru_code"] = "TEST-DRILL-MAINS"
    final = client.post("/api/v1/analyses", json=body).json()
    assert final["status"] == "resolved"
    assert final["selected_ktru_code"] == "TEST-DRILL-MAINS"
    assert final["catalog_version"] == "synthetic-test-only-v1"
    assert final["verification"]["confirmed_by_user"]
    assert all(candidate["ktru_code"] != "TEST-DRILL-FAMILY" for candidate in final["candidates"])
    assert "id" not in final


def test_single_candidate_without_confirmation(engine):
    result = request(engine, confirmed_category_id="tools")
    assert result.status == "needs_input"
    assert result.selected_ktru_code is None
    assert result.next_question.kind == "code"


def test_answer_correction_survives_category_reset(engine):
    description = "Дрель; питание: от сети"
    question = request(engine, description=description, confirmed_category_id="tools").next_question
    state = {"description": description, "answers": [{"question_id": question.id, "value": "650", "unit": "Вт"}],
             "attributes": [{"id": "edit", "name": "Мощность", "value": "700", "unit": "Вт", "replaces": "answer:" + question.id}]}
    reset = request(engine, **state)
    assert reset.next_question.kind == "category"
    assert any(fact.value == "700" for fact in reset.facts)
    final = request(engine, **state, confirmed_category_id="tools", confirmed_ktru_code="TEST-DRILL-MAINS")
    assert final.status == "resolved"
    assert [fact.value for fact in final.facts if fact.name == "Мощность"] == ["700"]


def test_question_offers_values_from_all_remaining_positions(engine):
    result = request(engine, description="Дрель", confirmed_category_id="tools")
    assert {option.value for option in result.next_question.options} == {"от сети", "от аккумулятора"}


def test_absent_optional_rule_not_needed_for_proven_unique_position(catalog_data):
    raw = catalog_data.model_dump(mode="json")
    raw["positions"][1]["properties"].append({"id": "unknown-rule", "name": "Сложное правило", "value_type": "unsupported",
        "required": False, "verified": True, "evidence": "test", "allowed": [{"kind": "enum", "value": "test", "source": "test"}]})
    result = request(runtime(Catalog.model_validate(raw)), confirmed_category_id="tools", confirmed_ktru_code="TEST-DRILL-MAINS")
    assert result.selected_ktru_code == "TEST-DRILL-MAINS"
    assert result.verification.value_verified
    reason = result.candidates[0].blocking_reasons[0]
    assert reason.applicability == "potential" and not reason.blocks_resolution


def test_category_change_and_fact_edit_rechecks_everything(engine):
    original = request(engine, confirmed_category_id="tools", confirmed_ktru_code="TEST-DRILL-MAINS")
    assert original.status == "resolved"
    power = next(fact for fact in original.facts if fact.name.casefold() == "мощность")
    changed = request(engine, confirmed_category_id="tools", attributes=[
        {"id": "edit", "name": "Мощность", "value": "900", "unit": "Вт", "replaces": power.id}])
    assert changed.selected_ktru_code is None
    assert changed.candidate_count == 0
    assert any(check.reason == "out_of_range" for candidate in changed.excluded for check in candidate.checks)
    other = request(engine, confirmed_category_id="furniture")
    assert other.selected_ktru_code is None
    assert other.scope.position_count == 1
    assert other.candidates[0].ktru_code == "TEST-TABLE"
    assert other.next_question.property_name == "Высота"


def test_additional_property_is_preserved_not_a_filter(engine):
    result = request(engine, confirmed_category_id="tools", confirmed_ktru_code="TEST-DRILL-MAINS",
                     attributes=[{"id": "extra", "name": "Цвет ручки", "value": "синий"}])
    assert result.status == "resolved"
    assert any("Цвет ручки" in fragment.raw for fragment in result.additional)


def test_number_without_unit_is_unknown_and_answer_supplies_unit(engine):
    description = "Дрель; питание: от сети; мощность: 650"
    result = request(engine, description=description, confirmed_category_id="tools")
    assert not result.verification.value_verified
    assert any(issue.code == "unit_missing" for issue in result.issues)
    answered = request(engine, description=description, confirmed_category_id="tools", answers=[
        {"question_id": result.next_question.id, "value": "650", "unit": "Вт"}], confirmed_ktru_code="TEST-DRILL-MAINS")
    assert answered.status == "resolved"


def test_unknown_answer_stays_unknown_and_is_not_asked_again(engine):
    description = "Дрель; питание: от сети"
    result = request(engine, description=description, confirmed_category_id="tools")
    answered = request(engine, description=description, confirmed_category_id="tools", answers=[
        {"question_id": result.next_question.id, "unknown": True}])
    assert answered.status == "needs_input"
    assert not answered.verification.value_verified
    assert answered.next_question is None
    assert answered.selected_ktru_code is None


def test_wrong_unit_and_incompatible_code_are_rejected(client):
    response = client.post("/api/v1/analyses", json={"description": "Дрель; питание: от сети; мощность: 650 кг",
                           "confirmed_category_id": "tools", "confirmed_ktru_code": "TEST-DRILL-MAINS"})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "incompatible_ktru_code"


@pytest.mark.parametrize("code", ["TEST-DRILL-FAMILY", "TEST-TABLE", "MISSING"])
def test_manual_code_does_not_change_scope(engine, code):
    with pytest.raises(InvalidConfirmation):
        request(engine, confirmed_category_id="tools", confirmed_ktru_code=code)


def test_conflicting_facts_require_correction(engine):
    result = request(engine, confirmed_category_id="tools", attributes=[
        {"id": "power2", "name": "Мощность", "value": "700", "unit": "Вт"}])
    assert result.status != "resolved"
    assert result.next_question.kind == "conflict"
    assert any(issue.code == "conflicting_facts" for issue in result.issues)


def test_identical_constraints_stop_questions_and_manual_choice_cannot_resolve(catalog_data):
    raw = catalog_data.model_dump(mode="json")
    duplicate = json.loads(json.dumps(raw["positions"][1]))
    duplicate.update(code="TEST-DRILL-IDENTICAL", name="Необоснованный подтип")
    raw["positions"].append(duplicate)
    engine = runtime(Catalog.model_validate(raw))
    result = request(engine, confirmed_category_id="tools", confirmed_ktru_code="TEST-DRILL-MAINS")
    assert result.status == "ambiguous"
    assert result.candidate_count == 2
    assert result.verification.value_indistinguishable
    assert result.next_question is None
    assert result.selected_ktru_code is None


def test_empty_rules_never_verify_values(engine):
    result = request(engine, description="Куртка рабочая", confirmed_category_id="clothes", confirmed_ktru_code="TEST-JACKET")
    assert result.status == "needs_data"
    assert not result.verification.value_verified
    assert result.selected_ktru_code is None


def test_complete_pool_is_checked_before_limit(catalog_data):
    raw = catalog_data.model_dump(mode="json")
    for index in range(25):
        duplicate = json.loads(json.dumps(raw["positions"][1]))
        duplicate.update(code=f"TEST-{index}")
        raw["positions"].append(duplicate)
    result = request(runtime(Catalog.model_validate(raw)), confirmed_category_id="tools", candidate_limit=1,
                     confirmed_ktru_code="TEST-DRILL-MAINS")
    assert len(result.candidates) == 1
    assert result.candidate_count == 26
    assert result.scope.evaluated_count == 27
    assert result.selected_ktru_code is None


def test_unverified_constraints_cannot_complete(catalog_data):
    raw = catalog_data.model_dump(mode="json")
    raw["positions"][1]["properties"][1]["verified"] = False
    result = request(runtime(Catalog.model_validate(raw)), confirmed_category_id="tools", confirmed_ktru_code="TEST-DRILL-MAINS")
    assert result.status != "resolved"
    assert not result.verification.value_verified


def test_incomplete_scope_cannot_complete(catalog_data):
    raw = catalog_data.model_dump(mode="json")
    raw["categories"][0]["coverage_verified"] = False
    result = request(runtime(Catalog.model_validate(raw)), confirmed_category_id="tools", confirmed_ktru_code="TEST-DRILL-MAINS")
    assert result.status == "needs_input"
    assert not result.verification.catalog_scope_complete


def test_safe_conversion(engine):
    result = request(engine, description="Дрель; питание: от сети; мощность: 0,65 кВт", confirmed_category_id="tools", confirmed_ktru_code="TEST-DRILL-MAINS")
    assert result.status == "resolved"
    assert any(correction.normalized == "650 Вт" for correction in result.corrections)


@pytest.mark.parametrize("unit,dimension", [("%", "percentage"), ("Ом", "resistance"), ("об/мин", "rotation_rate"),
                                           ("мин", "time"), ("м²", "area"), ("м³", "volume"), ("г/м²", "surface_density")])
def test_dimensions_are_not_conflated(unit, dimension):
    assert get_unit(unit).dimension == dimension


def test_minus_negation_identifiers_and_unparsed_survive():
    text = "  ДА-10,8 ГОСТ 123 (м²/м³); температура: −40 °C; без удара; неизвестная деталь"
    facts, fragments = extract(text, ("Температура", "Удара"))
    # Имя приводится к написанию из справочника, если форма слова подтверждена
    # леммами (см. app.extraction.canonical_name): «температура» -> «Температура».
    # Написание из текста не теряется — оно остаётся в raw и span.
    temperature = next(fact for fact in facts if canon(fact.name) == "температура")
    assert temperature.name == "Температура"
    assert temperature.raw == " температура: −40 °C"
    assert temperature.value == "−40"
    assert number(temperature.value).lower == Decimal(-40)
    assert any(fact.negated and fact.value == "нет" for fact in facts)
    assert any("ДА-10,8" in fragment.raw for fragment in fragments)
    for fact in facts:
        assert text[slice(*fact.span)] == fact.raw


def test_strict_boundaries_and_alternative_union():
    left = Interval(Decimal("-Infinity"), Decimal(19), False, False)
    right = Interval(Decimal(19), Decimal("Infinity"), True, False)
    assert not subset(number("19"), [left])
    assert subset(number("18"), [left])
    assert subset(number("-40"), [left, right])
    assert subset(number("19"), [left, right])
    assert subset(number("(-40; 40]"), [left, right])


def test_range_partial_overlap_is_not_compatible():
    assert not subset(number("450–650"), [number("500–800")])


@pytest.mark.parametrize("unit,wrong", [("%", "Ом"), ("об/мин", "мин"), ("г/м²", "кг")])
def test_compound_dimensions_and_value_codes(unit, wrong):
    prop = Property(id="test", name="Свойство", semantic_key="test", value_type="number", required=True, verified=True,
                    evidence="test", allowed=[{"kind":"number", "value":"20", "unit":unit, "raw_unit":unit,
                                              "value_code":"TEST-RAW-VALUE", "source":"test"}])
    fact = Fact(id="test", name="Свойство", value="20", unit=unit, raw="20", source="input")
    outcome, _, codes, _ = check_fact(prop, fact)
    assert outcome == "compatible" and codes == ["TEST-RAW-VALUE"]
    assert check_fact(prop, fact.model_copy(update={"unit":wrong}))[0] == "incompatible"


def test_mixed_units_are_not_unioned_as_trusted_constraints():
    prop = Property(id="test", name="Свойство", value_type="number", required=True, verified=True, evidence="test",
                    allowed=[{"kind":"number", "value":"20", "unit":unit, "source":"test"} for unit in ("%", "Ом")])
    fact = Fact(id="test", name="Свойство", value="20", unit="%", raw="20%", source="input")
    assert check_fact(prop, fact)[:2] == ("unknown", "unsupported_constraint")


def test_unparsed_measurement_requires_explicit_mapping(engine):
    description = "Дрель; питание: от сети; 650 Вт"
    result = request(engine, description=description, confirmed_category_id="tools")
    assert any(issue.code == "unmapped_measurement" for issue in result.issues)
    fragment = next(fact for fact in result.facts if "650" in fact.raw)
    fixed = request(engine, description=description, confirmed_category_id="tools", confirmed_ktru_code="TEST-DRILL-MAINS",
                    attributes=[{"id":"manual", "name":"Мощность", "value":"650", "unit":"Вт", "replaces":fragment.id}])
    assert fixed.status == "resolved"


def test_stale_correction_cannot_silently_remove_a_fact(engine):
    with pytest.raises(InvalidConfirmation):
        request(engine, attributes=[{"id":"bad", "name":"Мощность", "value":"700", "unit":"Вт", "replaces":"old-span"}])


def test_context_does_not_map_packaging_height_as_product(engine):
    result = request(engine, description="Стол", confirmed_category_id="furniture", attributes=[
        {"id": "packing", "name": "Высота", "value": "750", "unit": "мм", "subject": "packaging"}])
    assert not result.verification.fields_complete
    assert result.selected_ktru_code is None


def test_empty_catalog_returns_facts_and_needs_data():
    data = load_snapshot(__import__("pathlib").Path("resources/catalog"))
    result = analyze(data, None, AnalysisCreate(description="Мощность: 650 Вт"))
    assert result.status == "needs_data"
    assert result.facts[0].value == "650"
    assert result.catalog_version == "awaiting-validated-supply"


def test_client_cannot_supply_verification_proof(client):
    for extra in ({"verification": {"value_verified": True}}, {"normalized_value": "650"}):
        assert client.post("/api/v1/analyses", json={"description": "Дрель", **extra}).status_code == 422


def test_removed_stateful_routes_are_absent(client):
    for path in ("/api/v1/ste", "/api/v1/analyses/old", "/api/v1/analyses/old/feedback"):
        assert client.get(path).status_code == 404
    assert not any("/ste" in path or "feedback" in path for path in client.get("/api/openapi.json").json()["paths"])


def test_catalog_rejects_bad_references_quarantine_and_synthetic(catalog_data, tmp_path):
    for mutation in ("parent", "quarantine", "unit_number"):
        raw = catalog_data.model_dump(mode="json")
        if mutation == "parent": raw["positions"][1]["parent_code"] = "missing"
        elif mutation == "quarantine": raw["positions"][1]["quarantined"] = True
        else: raw["positions"][1]["properties"][1]["allowed"][0]["lower"] = "not a number"
        with pytest.raises(ValueError): Catalog.model_validate(raw)
    (tmp_path / "catalog.json").write_text(catalog_data.model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match="Synthetic"):
        load_snapshot(tmp_path)


def test_catalog_is_immutable_and_none_is_not_false(catalog_data):
    data = snapshot(catalog_data)
    assert data.positions["TEST-TABLE"].no_new_features is None
    with pytest.raises(TypeError): data.positions["bad"] = data.positions["TEST-TABLE"]
    with pytest.raises(ValueError): data.positions["TEST-TABLE"].name = "bad"
