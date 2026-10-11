DESCRIPTION = "Дрель; Питание: от сети; Мощность:650 Вт; Машность:900 Вт"
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
    assert any(fact["value"] == "900" and fact["name"] == "Мощность" and "Машность" in fact["raw"] for fact in response["facts"])


def test_rejection_preserves_additional_without_assigning_property(client):
    first = client.post("/api/v1/analyses", json=BODY).json()
    response = client.post("/api/v1/analyses", json={**BODY,"answers":[
        {"question_id":first["next_question"]["id"],"value":"reject"}]}).json()
    assert response["status"] == "resolved"
    typo = next(fact for fact in response["facts"] if fact["name"] == "Машность")
    assert typo["value"] == "900" and not typo["mappings"]
    assert any("Машность" in item["raw"] for item in response["additional"])


def test_unknown_correction_and_changed_value_remain_pending(client):
    first = client.post("/api/v1/analyses", json=BODY).json()
    answer = {"question_id":first["next_question"]["id"],"unknown":True}
    assert client.post("/api/v1/analyses", json={**BODY,"answers":[answer]}).json()["status"] == "needs_input"
    answer = {"question_id":first["next_question"]["id"],"value":"reject"}
    response = client.post("/api/v1/analyses", json={**BODY,"description":DESCRIPTION.replace("900", "901"),"answers":[answer]}).json()
    assert response["status"] == "needs_input"


def test_ordinary_additional_does_not_block_resolution(client):
    response = client.post("/api/v1/analyses", json={**BODY,"description":DESCRIPTION.split("; Машность")[0],
        "additional":["Доставка утром"],"attributes":[{"id":"note","name":"Комментарий","value":"Срочная поставка"}]}).json()
    assert response["status"] == "resolved"


def test_fact_edit_and_category_change_invalidate_old_correction(client):
    first = client.post("/api/v1/analyses", json=BODY).json()
    typo = next(fact for fact in first["facts"] if fact["name"] == "Машность")
    edited = {**BODY, "attributes":[{"id":"fixed", "name":"Мощность", "value":"650", "unit":"Вт", "replaces":typo["id"]}]}
    assert client.post("/api/v1/analyses", json=edited).json()["status"] == "resolved"
    changed = {"description":"Стол; Высота: 750 мм", "confirmed_category_id":"furniture", "confirmed_ktru_code":"TEST-TABLE",
               "answers":[{"question_id":first["next_question"]["id"],"value":"accept:Мощность"}]}
    response = client.post("/api/v1/analyses", json=changed).json()
    assert response["status"] == "resolved" and response["selected_ktru_code"] == "TEST-TABLE"


def test_typo_in_a_bare_name_is_offered_for_confirmation(client):
    """«Высата 750 мм» без двоеточия: раньше факта не было вовсе, и предлагать было нечего.

    Извлечение находило только имена из справочника, поэтому опечатка молча уходила
    в неразобранные фрагменты. Теперь факт создаётся **с написанием пользователя**,
    а поток коррекций показывает «Это «Высота»» — решает человек, подстановки нет.
    """
    body = {"description": "Стол; Высата 750 мм", "confirmed_category_id": "furniture"}
    first = client.post("/api/v1/analyses", json=body).json()
    typo = next(fact for fact in first["facts"] if fact["name"] == "Высата")
    assert typo["value"] == "750" and typo["unit"] == "мм"
    assert any(correction["original"] == "Высата" and correction["normalized"] == "Высота"
               and correction["confirmation_required"] for correction in first["corrections"])

    question = first["next_question"]
    assert question["kind"] == "correction"
    assert "Возможно, вы имели в виду" in question["title"]
    assert any(option["value"] == "accept:Высота" for option in question["options"])

    accepted = client.post("/api/v1/analyses", json={**body, "answers": [
        {"question_id": question["id"], "value": "accept:Высота"}]}).json()
    assert any(fact["name"] == "Высота" and fact["value"] == "750" for fact in accepted["facts"])
    assert not any(fact["name"] == "Высата" for fact in accepted["facts"])


def test_homoglyph_typo_in_a_bare_name_is_offered_too(client):
    """«Высaта» с латинской «a» — та же дорога: вопрос-коррекция, а не подстановка."""
    body = {"description": "Стол; Высaта 750 мм", "confirmed_category_id": "furniture"}
    first = client.post("/api/v1/analyses", json=body).json()
    question = first["next_question"]
    assert question["kind"] == "correction"
    assert any(option["value"] == "accept:Высота" for option in question["options"])


def test_unsure_typo_is_offered_for_confirmation(client):
    """«Машность» (похожесть 0.75) — уровень подсказки: вопрос без подстановки."""
    first = client.post("/api/v1/analyses", json=BODY).json()
    assert first["next_question"]["kind"] == "correction"
    assert "Возможно, вы имели в виду" in first["next_question"]["title"]


def test_confident_typo_is_replaced_and_reported(client):
    """«Мошность» (0.875) — уровень автозамены: имя заменено, вопроса нет.

    Замена не молчит: в ответе остаётся провенанс «Мошность» → «Мощность»
    с `confirmation_required=False`, а исходное написание — в `raw` факта.
    """
    body = {"description": "Дрель; Питание: от сети; Мошность 650 Вт",
            "confirmed_category_id": "tools"}
    first = client.post("/api/v1/analyses", json=body).json()
    fact = next(item for item in first["facts"] if item["name"] == "Мощность")
    assert fact["value"] == "650" and "Мошность" in fact["raw"]
    assert not any(item["name"] == "Мошность" for item in first["facts"])
    assert any(correction["original"] == "Мошность" and correction["normalized"] == "Мощность"
               and not correction["confirmation_required"] for correction in first["corrections"])
    assert (first.get("next_question") or {}).get("kind") != "correction"
