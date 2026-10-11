def test_excluding_text_fact_preserves_description_and_requires_value_again(client):
    body = {"description": "Дрель; Питание: от сети; Мощность: 650 Вт", "confirmed_category_id": "tools"}
    first = client.post("/api/v1/analyses", json=body).json()
    power = next(f for f in first["facts"] if f["name"] == "Мощность")
    removed = client.post("/api/v1/analyses", json={**body, "excluded_fact_ids": [power["id"]]})
    assert removed.status_code == 200
    result = removed.json()
    assert not any(f["id"] == power["id"] for f in result["facts"])
    assert result["original_text"] == body["description"]
    assert result["selected_ktru_code"] is None
    assert not result["verification"]["fields_complete"]
    assert result["next_question"]["property_name"] == "Мощность"
    repeated = client.post("/api/v1/analyses", json={**body, "excluded_fact_ids": [power["id"]]}).json()
    assert repeated["facts"] == result["facts"]


def test_excluded_input_is_not_used_for_code_confirmation(client):
    body = {"description": "Дрель; Питание: от сети", "confirmed_category_id": "tools",
            "attributes": [{"id": "input:power", "name": "Мощность", "value": "650", "unit": "Вт"}],
            "excluded_fact_ids": ["input:power"], "confirmed_ktru_code": "TEST-DRILL-MAINS"}
    result = client.post("/api/v1/analyses", json=body).json()
    assert not any(f["id"] == "input:power" for f in result["facts"])
    assert result["selected_ktru_code"] is None
    assert not result["verification"]["fields_complete"]


def test_excluded_answer_can_be_asked_again(client):
    body = {"description": "Дрель", "confirmed_category_id": "tools"}
    question = client.post("/api/v1/analyses", json=body).json()["next_question"]
    result = client.post("/api/v1/analyses", json={**body, "answers": [{"question_id": question["id"], "value": "от сети"}],
        "excluded_fact_ids": ["answer:" + question["id"]]}).json()
    assert result["next_question"]["id"] == question["id"]
    assert not any(f["id"] == "answer:" + question["id"] for f in result["facts"])
