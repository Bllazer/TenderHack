"""Read-only smoke of Compose with either the clean supply or an empty catalog."""
import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def fetch(base, path, body=None):
    request = Request(base + path, data=json.dumps(body, ensure_ascii=False).encode() if body else None,
                      headers={"Content-Type": "application/json"})
    try:
        response = urlopen(request, timeout=20)
    except HTTPError as exc:
        response = exc
    with response:
        raw = response.read()
        return response.status, json.loads(raw) if "json" in response.headers.get("Content-Type", "") else raw.decode()


if __name__ == "__main__":
    for base in (os.environ.get("SMOKE_API", "http://127.0.0.1:8000"),
                 os.environ.get("SMOKE_FRONTEND", "http://127.0.0.1:8080")):
        assert fetch(base, "/api/health/live") == (200, {"status": "ok"})
        status, ready = fetch(base, "/api/health/ready")
        assert status == 503
        status, catalog = fetch(base, "/api/v1/catalog/status")
        assert status == 200 and not catalog["ready"]
        if catalog["catalog_loaded"]:
            assert catalog["positions"] == 60087 and catalog["templates"] == 5897 and catalog["final_positions"] == 54190
            assert ready["detail"]["code"] == "search_index_missing"
            assert ready["detail"]["missing_resources"] == ["category_mapping", "search_index"]
            status, actual = fetch(base, "/api/v1/analyses", {"description":"Чай черный (ферментированный)",
                "confirmed_ktru_code":"10.83.13.120-00000003"})
            assert status == 200 and actual["status"] == "needs_data" and actual["selected_ktru_code"] is None
            assert not actual["verification"]["value_verified"] and not actual["verification"]["type_verified"]
            assert fetch(base, "/api/v1/analyses", {"description":"Чай", "confirmed_ktru_code":"10.83.13.120-00000001"})[0] == 422
        else:
            assert catalog["positions"] == 0 and ready["detail"]["code"] == "catalog_empty"
        status, result = fetch(base, "/api/v1/analyses", {"description": "Температура: −40 °C; мощность: 650 Вт"})
        assert status == 200 and result["status"] == "needs_data" and result["selected_ktru_code"] is None
        assert result["facts"][0]["value"] == "−40"
        assert result["catalog_version"] == catalog["catalog_version"]
        assert fetch(base, "/api/v1/analyses/old")[0] == 404
        status, schema = fetch(base, "/api/openapi.json")
        assert status == 200 and schema["info"]["version"] == "0.2.0"
        print(base + ": live=200 ready=503(" + ready["detail"]["code"] + ") positions=" + str(catalog["positions"]) + " analysis=needs_data routes/OpenAPI=OK")
    status, html = fetch(os.environ.get("SMOKE_FRONTEND", "http://127.0.0.1:8080"), "/")
    assert status == 200 and 'id="root"' in html
    print("Compose frontend HTML=200; /api proxy preserves prefix")
