"""Проверка пакетного разбора CSV и целостности API на живом сервисе.

Запуск из backend/ (нужен поднятый API):
    python -X utf8 tests/batch_check.py
    python -X utf8 tests/batch_check.py --base http://127.0.0.1:8000

Только стандартная библиотека. Проверяет и новый маршрут, и то, что одиночный
анализ после правок продолжает работать.
"""
from __future__ import annotations

import argparse
import base64
import json
import pathlib
import sys
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
SAMPLE = HERE / "sample_batch.csv"
STATUSES = {"needs_data", "needs_input", "ambiguous", "resolved", "error"}

RESULTS = []


def check(title, condition, detail=""):
    RESULTS.append((bool(condition), title, detail))
    print("%-4s %s%s" % ("OK" if condition else "FAIL", title, "" if condition else " -> %s" % detail))


def call(base, path, payload=None, method="POST", timeout=600):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(base.rstrip("/") + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", "replace")
        try:
            return error.code, json.loads(raw)
        except json.JSONDecodeError:
            return error.code, {"raw": raw[:300]}
    except Exception as error:  # сервис недоступен
        return None, {"transport_error": "%s: %s" % (type(error).__name__, error)}


def error_code(body):
    detail = (body or {}).get("detail")
    return detail.get("code") if isinstance(detail, dict) else None


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    args = parser.parse_args(argv)
    base = args.base

    raw = SAMPLE.read_bytes()
    text = raw.decode("utf-8")
    cp1251 = base64.b64encode(text.encode("cp1251")).decode("ascii")

    status, ready = call(base, "/api/health/ready", method="GET")
    check("API готов (200)", status == 200 and ready.get("status") == "ok", "%s %s" % (status, ready))
    check("движок v5", ready.get("engine_version") == "stateless-pipeline-v5-context-card",
          ready.get("engine_version"))

    status, batch = call(base, "/api/v1/analyses/batch", {"csv": text, "filename": "sample_batch.csv"})
    check("CSV текстом: HTTP 200", status == 200, "%s %s" % (status, str(batch)[:200]))
    if status == 200:
        check("кодировка utf-8", batch["encoding"] == "utf-8", batch["encoding"])
        check("разделитель ';'", batch["delimiter"] == ";", batch["delimiter"])
        check("заголовок пропущен", batch["header_skipped"] is True, batch["header_skipped"])
        check("5 строк товара", batch["rows_total"] == 5 and len(batch["rows"]) == 5,
              "%s / %s" % (batch["rows_total"], len(batch["rows"])))
        check("у каждой строки статус", all(row["status"] in STATUSES for row in batch["rows"]),
              [row["status"] for row in batch["rows"]])
        check("описания не пустые", all(row["description"].strip() for row in batch["rows"]), "")
        check("сводка counts согласована", batch["counts"]["rows"] == len(batch["rows"]),
              str(batch["counts"]))
        check("все строки имеют причину или resolved",
              all(row["reason"] or row["status"] == "resolved" for row in batch["rows"]),
              [(row["status"], row["reason"]) for row in batch["rows"]])
        check("resolved-строки с кодом",
              all(row["selected_ktru_code"] for row in batch["rows"] if row["status"] == "resolved"), "")
        check("причина confirm_code_required только при вопросе о коде",
              all(row["question_kind"] == "code" for row in batch["rows"]
                  if row["reason"] == "confirm_code_required"), "")
        print("\n  строки файла:")
        for row in batch["rows"]:
            print("    %2d | %-11s | код %-26s | %-24s | %s" % (
                row["line"], row["status"], row["selected_ktru_code"] or "-",
                (row["question_property"] or row["question_kind"] or "-")[:24], row["reason"] or "-"))
        print("  сводка: %s\n" % json.dumps(batch["counts"], ensure_ascii=False))

    status, encoded = call(base, "/api/v1/analyses/batch", {"csv_base64": base64.b64encode(raw).decode("ascii")})
    check("CSV в base64: те же строки", status == 200
          and [row["description"] for row in encoded["rows"]] == [row["description"] for row in batch["rows"]],
          "%s %s" % (status, str(encoded)[:160]))

    status, cp = call(base, "/api/v1/analyses/batch", {"csv_base64": cp1251})
    check("cp1251 читается без кракозябр", status == 200 and cp["encoding"] == "cp1251"
          and all("Р" not in row["description"][:2] for row in cp["rows"]),
          "%s %s %s" % (status, cp.get("encoding"), [row["description"][:18] for row in cp.get("rows", [])]))

    status, limited = call(base, "/api/v1/analyses/batch", {"csv": text, "run_limit": 2})
    check("run_limit ограничивает строки", status == 200 and len(limited["rows"]) == 2
          and limited["truncated"] is True, "%s %s" % (status, str(limited)[:160]))

    status, empty = call(base, "/api/v1/analyses/batch", {"csv": "описание\n\n"})
    check("файл только с заголовком -> 422 csv_empty", status == 422 and error_code(empty) == "csv_empty",
          "%s %s" % (status, empty))

    status, broken = call(base, "/api/v1/analyses/batch", {"csv_base64": "!!!не base64!!!"})
    check("битый base64 -> 422 csv_invalid", status == 422 and error_code(broken) == "csv_invalid",
          "%s %s" % (status, broken))

    status, both = call(base, "/api/v1/analyses/batch", {"csv": text, "csv_base64": cp1251})
    check("csv и csv_base64 вместе -> 422", status == 422, "%s" % status)

    # --- маршрут «только разобрать файл»: по нему страница получает строки и дальше
    # --- прогоняет каждую обычным одиночным анализом
    status, parsed = call(base, "/api/v1/analyses/csv", {"csv": text, "filename": "sample_batch.csv"})
    check("разбор файла: HTTP 200", status == 200, "%s %s" % (status, str(parsed)[:200]))
    if status == 200:
        check("разбор файла: 5 строк с номерами и описаниями",
              parsed["rows_total"] == 5 and len(parsed["rows"]) == 5
              and all(row["line"] and row["description"].strip() for row in parsed["rows"]),
              str(parsed["rows"])[:200])
        check("разбор файла: заголовок и разделитель",
              parsed["header_skipped"] is True and parsed["delimiter"] == ";", str(parsed)[:160])
        check("разбор файла: строки совпадают с пакетным маршрутом",
              [row["description"] for row in parsed["rows"]] == [row["description"] for row in batch["rows"]], "")

    status, parsed_cp = call(base, "/api/v1/analyses/csv", {"csv_base64": cp1251})
    check("разбор файла: cp1251 без кракозябр", status == 200 and parsed_cp["encoding"] == "cp1251"
          and parsed_cp["rows"][0]["description"].startswith("Халат"), "%s %s" % (status, parsed_cp.get("rows", [])[:1]))

    status, parsed_empty = call(base, "/api/v1/analyses/csv", {"csv": "описание\n"})
    check("разбор файла: только заголовок -> 422 csv_empty",
          status == 422 and error_code(parsed_empty) == "csv_empty", "%s %s" % (status, parsed_empty))

    status, single = call(base, "/api/v1/analyses", {
        "description": "Киянка", "confirmed_category_id": "25.73.30.290-00000004",
        "attributes": [{"id": "sel", "name": "Форма сечения рабочей части", "values": ["Круг"]}],
        "confirmed_ktru_code": "25.73.30.290-00000003"})
    check("одиночный анализ по-прежнему доходит до resolved",
          status == 200 and single.get("status") == "resolved"
          and single.get("selected_ktru_code") == "25.73.30.290-00000003",
          "%s %s %s" % (status, single.get("status"), single.get("selected_ktru_code")))

    status, blocked = call(base, "/api/v1/catalog/status", method="GET")
    check("каталог на месте", status == 200 and blocked.get("ready") is True
          and blocked.get("positions") == 60087, "%s %s" % (status, blocked))

    failed = [title for ok, title, _ in RESULTS if not ok]
    print("\nвсего %d, провалено %d%s" % (len(RESULTS), len(failed), (": " + "; ".join(failed)) if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
