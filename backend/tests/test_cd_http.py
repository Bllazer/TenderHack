from conftest import runtime


def test_http_runs_c_and_d_with_one_lifespan_index(client, monkeypatch):
    import app.main as main
    import app.algorithm_adapter as adapter
    calls = []
    c, d = adapter.run_c, adapter.algorithm_question
    def check(*args, **kwargs):
        calls.append("C")
        return c(*args, **kwargs)
    def question(*args, **kwargs):
        calls.append("D")
        return d(*args, **kwargs)
    monkeypatch.setattr(adapter, "run_c", check)
    monkeypatch.setattr(adapter, "algorithm_question", question)
    index = main.app.state.components.checker.index
    response = client.post("/api/v1/analyses", json={"description":"Стол", "confirmed_category_id":"furniture"}).json()
    assert response["status"] == "needs_input" and response["next_question"]["property_name"] == "Высота"
    assert {"C", "D"} <= set(calls)
    assert main.app.state.components.checker.index is index


def test_question_includes_all_choices_and_unknown_keeps_uncertainty(client):
    body = {"description":"Дрель", "confirmed_category_id":"tools"}
    result = client.post("/api/v1/analyses", json=body).json()
    assert result["candidate_count"] == 2
    question = result["next_question"]
    assert question["property_name"] == "Питание"
    assert {item["value"] for item in question["options"]} == {"от сети", "от аккумулятора"}
    result = client.post("/api/v1/analyses", json={**body,"answers":[{"question_id":question["id"],"unknown":True}]}).json()
    assert result["selected_ktru_code"] is None and not result["verification"]["value_verified"]


def test_unverified_competitor_does_not_prove_uniqueness(catalog_data, monkeypatch):
    import app.main as main
    from fastapi.testclient import TestClient
    table = next(p for p in catalog_data.positions if p.code == "TEST-TABLE")
    rival = table.model_copy(update={"code":"TEST-UNKNOWN", "source_issues":('"source defect"',)})
    categories = tuple(c.model_copy(update={"position_codes":("TEST-TABLE", "TEST-UNKNOWN")}) if c.id == "furniture" else c
                       for c in catalog_data.categories)
    data = catalog_data.model_copy(update={"positions":(*catalog_data.positions,rival),"categories":categories})
    monkeypatch.setattr(main, "resources", lambda:runtime(data))
    with TestClient(main.app) as api:
        response = api.post("/api/v1/analyses", json={"description":"Стол; Высота: 750 мм", "confirmed_category_id":"furniture",
            "confirmed_ktru_code":"TEST-TABLE"}).json()
        assert response["candidate_count"] == 2 and not response["verification"]["ktru_unique"]
        assert response["selected_ktru_code"] is None


def strict_bound_catalog(catalog_data):
    """Каталог, у которого нижняя граница высоты строгая: (700;800] мм."""
    table = next(p for p in catalog_data.positions if p.code == "TEST-TABLE")
    strict = next(prop for prop in table.properties if prop.name == "Высота").allowed[0].model_copy(
        update={"lower_inclusive": False})
    table = table.model_copy(update={"properties": tuple(
        prop.model_copy(update={"allowed": (strict,)}) if prop.name == "Высота" else prop
        for prop in table.properties)})
    return catalog_data.model_copy(update={"positions": tuple(
        table if p.code == "TEST-TABLE" else p for p in catalog_data.positions)})


def test_http_open_range_against_strict_catalog_bound_does_not_dead_end(catalog_data, monkeypatch):
    """Открытый диапазон против строгой границы справочника не должен упираться.

    Раньше C не читал min_inclusive/max_inclusive, считал требование (700;800]
    замкнутым [700;800], натыкался на строгую границу справочника и отвечал unknown.
    Мост понижал подтверждённое значение до непроверенного, и HTTP возвращал
    needs_input БЕЗ вопроса: пользователю нечего было уточнить.
    """
    import app.main as main
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main, "resources", lambda: runtime(strict_bound_catalog(catalog_data)))
    with TestClient(main.app) as api:
        body = {"description": "Стол; Высота: (700;800] мм", "confirmed_category_id": "furniture",
                "confirmed_ktru_code": "TEST-TABLE"}
        response = api.post("/api/v1/analyses", json=body).json()
        assert response["status"] == "resolved", response
        assert response["selected_ktru_code"] == "TEST-TABLE"
        assert response["verification"]["value_verified"]

        # Строгость уважается и в обратную сторону: [700;800] включает 700,
        # которое справочник исключает, — такое подтверждение отклоняется.
        wrong = api.post("/api/v1/analyses", json={**body, "description": "Стол; Высота: [700;800] мм"})
        assert wrong.status_code == 422, wrong.text

        # Без подтверждённого кода пользователь получает вопрос, а не пустой ответ.
        unconfirmed = api.post("/api/v1/analyses", json={
            "description": "Стол; Высота: (700;800] мм", "confirmed_category_id": "furniture"}).json()
        assert unconfirmed["status"] == "needs_input"
        assert unconfirmed["next_question"] is not None
