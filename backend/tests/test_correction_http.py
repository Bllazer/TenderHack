DESCRIPTION = "Дрель; Питание: от сети; Мощность:650 Вт; Мошность:900 Вт"
BODY = {"description": DESCRIPTION, "confirmed_category_id":"tools", "confirmed_ktru_code":"TEST-DRILL-MAINS"}


def test_unresolved_correction_blocks_confirmation(client):
    response = client.post("/api/v1/analyses", json=BODY).json()
    assert response["status"] == "needs_input" and response["selected_ktru_code"] is None
    assert response["verification"]["blocking_issues"]
    assert response["next_question"]["kind"] == "correction"
    assert any(option["value"] == "accept:Мощность" for option in response["next_question"]["options"])


def test_accepting_correction_rechecks_and_detects_conflict(client):
    first = client.post("/api/v1/analyses", json=BODY).json()
    response = client.post("/api/v1/analyses", json={**BODY,"answers":[
        {"question_id":first["next_question"]["id"],"value":"accept:Мощность"}]}).json()
    assert response["status"] == "needs_input" and response["selected_ktru_code"] is None
    assert any(issue["code"] == "conflicting_facts" for issue in response["issues"])
    assert any(fact["value"] == "900" and fact["name"] == "Мощность" and "Мошность" in fact["raw"] for fact in response["facts"])


def test_rejection_preserves_additional_without_assigning_property(client):
    first = client.post("/api/v1/analyses", json=BODY).json()
    response = client.post("/api/v1/analyses", json={**BODY,"answers":[
        {"question_id":first["next_question"]["id"],"value":"reject"}]}).json()
    assert response["status"] == "resolved"
    typo = next(fact for fact in response["facts"] if fact["name"] == "Мошность")
    assert typo["value"] == "900" and not typo["mappings"]
    assert any("Мошность" in item["raw"] for item in response["additional"])


def test_unknown_correction_and_changed_value_remain_pending(client):
    first = client.post("/api/v1/analyses", json=BODY).json()
    answer = {"question_id":first["next_question"]["id"],"unknown":True}
    assert client.post("/api/v1/analyses", json={**BODY,"answers":[answer]}).json()["status"] == "needs_input"
    answer = {"question_id":first["next_question"]["id"],"value":"reject"}
    response = client.post("/api/v1/analyses", json={**BODY,"description":DESCRIPTION.replace("900", "901"),"answers":[answer]}).json()
    assert response["status"] == "needs_input"


def test_ordinary_additional_does_not_block_resolution(client):
    response = client.post("/api/v1/analyses", json={**BODY,"description":DESCRIPTION.split("; Мошность")[0],
        "additional":["Доставка утром"],"attributes":[{"id":"note","name":"Комментарий","value":"Срочная поставка"}]}).json()
    assert response["status"] == "resolved"


def test_fact_edit_and_category_change_invalidate_old_correction(client):
    first = client.post("/api/v1/analyses", json=BODY).json()
    typo = next(fact for fact in first["facts"] if fact["name"] == "Мошность")
    edited = {**BODY, "attributes":[{"id":"fixed", "name":"Мощность", "value":"650", "unit":"Вт", "replaces":typo["id"]}]}
    assert client.post("/api/v1/analyses", json=edited).json()["status"] == "resolved"
    changed = {"description":"Стол; Высота: 750 мм", "confirmed_category_id":"furniture", "confirmed_ktru_code":"TEST-TABLE",
               "answers":[{"question_id":first["next_question"]["id"],"value":"accept:Мощность"}]}
    response = client.post("/api/v1/analyses", json=changed).json()
    assert response["status"] == "resolved" and response["selected_ktru_code"] == "TEST-TABLE"
