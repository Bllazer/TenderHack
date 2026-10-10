"""Пользовательские приёмочные сценарии TenderHack (пайплайн T04/T14).

Две независимые проверки:

1. `run`    — человеческие сценарии из tests/user_cases.json: описание -> тип ->
              вопрос -> правка -> подтверждение кода. Проверяются и ожидания кейса,
              и общие инварианты контракта.
2. `replay` — 200+ реальных запросов, записанных в docs/reviews/**/*.json вместе с
              ответами на каталоге clean5. Запрос повторяется, ответ сверяется по
              сводным полям. Ничего не выдумывается: ожидание = записанный ответ.

Запуск из backend/ (нужен работающий API, каталог и модель):

    python -X utf8 tests/user_cases.py list
    python -X utf8 tests/user_cases.py run    --base http://127.0.0.1:8000
    python -X utf8 tests/user_cases.py replay --base http://127.0.0.1:8000
    python -X utf8 tests/user_cases.py run --only kilanka --verbose --json .tools/user_cases.json

Зависимостей нет — только стандартная библиотека, как в пакете algorithms.
"""
from __future__ import annotations

import argparse
import ast
import json
import pathlib
import sys
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
CASES = HERE / "user_cases.json"
REVIEWS = HERE.parent / "docs" / "reviews"

VERIFICATION_KEYS = ("type_verified", "ktru_unique", "fields_complete", "value_verified",
                     "catalog_scope_complete", "value_indistinguishable", "confirmed_by_user",
                     "blocking_issues")


# --------------------------------------------------------------------------- http
def request(base: str, method: str, path: str, payload=None, timeout: float = 120.0):
    """Возвращает (http_status, body). Тело — разобранный JSON или текст."""
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", "replace")
        status = error.code
    except Exception as error:  # сервис недоступен
        return None, {"transport_error": "%s: %s" % (type(error).__name__, error)}
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, {"raw": raw[:400]}


def post_analysis(base: str, payload: dict):
    return request(base, "POST", "/api/v1/analyses", payload)


# ------------------------------------------------------------------- инварианты
def invariants(body: dict) -> list:
    """Общие требования контракта, независимые от конкретного кейса."""
    problems = []
    if not isinstance(body, dict) or "status" not in body:
        return problems
    status = body.get("status")
    selected = body.get("selected_ktru_code")
    verification = body.get("verification") or {}
    scope = body.get("scope") or {}
    question = body.get("next_question")
    if selected and status != "resolved":
        problems.append("код выбран при статусе %s" % status)
    if status == "resolved":
        if not selected:
            problems.append("resolved без selected_ktru_code")
        for key in ("type_verified", "ktru_unique", "fields_complete", "value_verified",
                    "catalog_scope_complete", "confirmed_by_user"):
            if verification.get(key) is not True:
                problems.append("resolved при %s=%r" % (key, verification.get(key)))
        if verification.get("blocking_issues"):
            problems.append("resolved при blocking_issues=true")
        if question:
            problems.append("resolved и вопрос одновременно")
    if verification.get("value_indistinguishable") and selected:
        problems.append("выбран код при value_indistinguishable=true")
    if question and question.get("kind") == "code" and body.get("candidate_count") != 1:
        problems.append("вопрос о коде при candidate_count=%s" % body.get("candidate_count"))
    if scope and scope.get("position_count") is not None and scope.get("evaluated_count") is not None:
        if scope["evaluated_count"] != scope["position_count"]:
            problems.append("проверено %s из %s позиций области" % (scope["evaluated_count"], scope["position_count"]))
    for candidate in body.get("candidates") or []:
        if candidate.get("outcome") == "incompatible":
            problems.append("несовместимая позиция %s осталась в candidates" % candidate.get("ktru_code"))
    if body.get("candidate_count") is not None and body.get("candidate_limit") is None:
        if body["candidate_count"] < len(body.get("candidates") or []):
            problems.append("candidate_count меньше списка кандидатов")
    return problems


def classify(body: dict) -> str:
    """Одна причина незавершения в терминах пайплайна (наша классификация, не API)."""
    if not isinstance(body, dict) or body.get("status") in (None, "resolved"):
        return "-"
    if body.get("transport_error"):
        return "service_unavailable"
    issues = {issue.get("code") for issue in body.get("issues") or []}
    verification = body.get("verification") or {}
    candidates = body.get("candidates") or []
    excluded = body.get("excluded") or []
    if "conflicting_facts" in issues:
        return "conflicting_facts"
    if verification.get("value_indistinguishable"):
        return "value_indistinguishable"
    question = body.get("next_question") or {}
    if question.get("kind") == "code":
        return "confirm_code_required"
    if question.get("kind") == "conflict":
        return "conflicting_facts"
    category = (body.get("scope") or {}).get("category_id")
    if not category:
        return "type_unknown"
    if body.get("status") == "needs_data":
        return "unsupported_constraint" if "catalog_constraints_unavailable" in issues else "catalog_link_missing"
    blocking = [reason.get("code") for candidate in candidates for reason in candidate.get("blocking_reasons") or []
                if reason.get("blocks_resolution")]
    if "unsupported_constraint" in issues or blocking:
        return "unsupported_constraint"
    if not candidates and excluded:
        return "no_compatible_candidate"
    if not candidates:
        return "catalog_link_missing"
    # Неразобранным считается измерение или значение без сопоставления, а не сам текст описания.
    if "unmapped_measurement" in issues or any(
            issue in issues for issue in ("unresolved_property_correction", "unresolved_value_correction",
                                          "mapping_ambiguous")):
        return "mapping_missing"
    if any(issue in issues for issue in ("unit_missing", "unit_unknown", "unit_mismatch",
                                         "numeric_semantics_unknown")):
        return "mapping_missing"
    if any(candidate.get("missing_required") for candidate in candidates):
        return "missing_required"
    if any(candidate.get("outcome") == "unknown" for candidate in candidates):
        return "verification_incomplete"
    return "confirm_code_required" if len(candidates) == 1 else "value_indistinguishable"


# ------------------------------------------------------------------ ожидания
def check_expect(body: dict, expect: dict, http: int, where: str) -> tuple:
    ok, notes = True, []
    if "http" in expect and http != expect["http"]:
        return False, ["%s: HTTP %s вместо %s" % (where, http, expect["http"])]
    if "error_code" in expect:
        detail = (body or {}).get("detail")
        code = detail.get("code") if isinstance(detail, dict) else None
        if code != expect["error_code"]:
            ok = False
            notes.append("%s: код ошибки %r вместо %r" % (where, code, expect["error_code"]))
    if "status" in expect and body.get("status") != expect["status"]:
        ok = False
        notes.append("%s: статус %r вместо %r" % (where, body.get("status"), expect["status"]))
    if "status_in" in expect and body.get("status") not in expect["status_in"]:
        ok = False
        notes.append("%s: статус %r не в %s" % (where, body.get("status"), expect["status_in"]))
    if "status_exclude" in expect and body.get("status") in expect["status_exclude"]:
        ok = False
        notes.append("%s: недопустимый статус %r" % (where, body.get("status")))
    if "selected_ktru_code" in expect and body.get("selected_ktru_code") != expect["selected_ktru_code"]:
        ok = False
        notes.append("%s: selected=%r вместо %r" % (where, body.get("selected_ktru_code"),
                                                    expect["selected_ktru_code"]))
    if "candidate_count" in expect and body.get("candidate_count") != expect["candidate_count"]:
        ok = False
        notes.append("%s: кандидатов %s вместо %s" % (where, body.get("candidate_count"),
                                                      expect["candidate_count"]))
    if "candidate_count_max" in expect and (body.get("candidate_count") or 0) > expect["candidate_count_max"]:
        ok = False
        notes.append("%s: кандидатов %s, ожидалось не больше %s" % (where, body.get("candidate_count"),
                                                                    expect["candidate_count_max"]))
    if "excluded_count_min" in expect and (body.get("excluded_count") or 0) < expect["excluded_count_min"]:
        ok = False
        notes.append("%s: исключено %s, ожидалось не меньше %s" % (where, body.get("excluded_count"),
                                                                   expect["excluded_count_min"]))
    if "categories_max" in expect and len(body.get("categories") or []) > expect["categories_max"]:
        ok = False
        notes.append("%s: предложено %s категорий, ожидалось не больше %s" % (
            where, len(body.get("categories") or []), expect["categories_max"]))
    if "category_first" in expect:
        first = (body.get("categories") or [{}])[0].get("id")
        if first != expect["category_first"]:
            ok = False
            notes.append("%s: первая предложенная категория %r вместо %r" % (where, first, expect["category_first"]))
    codes = {candidate.get("ktru_code") for candidate in body.get("candidates") or []}
    for code in expect.get("candidates_include") or []:
        if code not in codes:
            ok = False
            notes.append("%s: верный код %s потерян (его нет среди кандидатов)" % (where, code))
    for code in expect.get("candidates_exclude") or []:
        if code in codes:
            ok = False
            notes.append("%s: код %s не должен оставаться кандидатом" % (where, code))
    excluded_codes = {candidate.get("ktru_code") for candidate in body.get("excluded") or []}
    for code in expect.get("excluded_include") or []:
        if code not in excluded_codes:
            ok = False
            notes.append("%s: код %s должен быть в excluded" % (where, code))
    if "question" in expect:
        wanted = expect["question"]
        question = body.get("next_question")
        if wanted is None:
            if question:
                ok = False
                notes.append("%s: вопрос не ожидался, получен %r" % (where, question.get("kind")))
        else:
            if not question:
                ok = False
                notes.append("%s: вопроса нет, ожидался %s" % (where, wanted))
            else:
                for key in ("kind", "property_name"):
                    if key in wanted and question.get(key) != wanted[key]:
                        ok = False
                        notes.append("%s: вопрос %s=%r вместо %r" % (where, key, question.get(key), wanted[key]))
    for key, value in (expect.get("verification") or {}).items():
        actual = (body.get("verification") or {}).get(key)
        if actual != value:
            ok = False
            notes.append("%s: verification.%s=%r вместо %r" % (where, key, actual, value))
    issue_codes = {issue.get("code") for issue in body.get("issues") or []}
    for code in expect.get("issues_include") or []:
        if code not in issue_codes:
            ok = False
            notes.append("%s: нет issue %s (есть: %s)" % (where, code, sorted(issue_codes)))
    for code in expect.get("issues_exclude") or []:
        if code in issue_codes:
            ok = False
            notes.append("%s: не ожидался issue %s" % (where, code))
    for rule in expect.get("facts_not") or []:
        for fact in body.get("facts") or []:
            if fact.get("value") == rule.get("value") and fact.get("unit") == rule.get("unit"):
                ok = False
                notes.append("%s: факт %r получил запрещённую единицу %r" % (where, fact.get("value"), fact.get("unit")))
    return ok, notes


def merge(payload: dict, defaults: dict) -> dict:
    merged = dict(defaults or {})
    merged.update(payload or {})
    return merged


def write_json(path, payload) -> None:
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def answer_from_question(payload: dict, remembered: dict, last: dict) -> tuple:
    """Собирает ответ пользователя: по захваченному вопросу, по свойству или первый доступный."""
    question = None
    key = payload.get("question")
    if key and key in remembered:
        question = remembered[key]
    elif key:
        return None, "неизвестный вопрос %r" % key
    if question is None and payload.get("property"):
        question = last.get("next_question")
        if not question or question.get("property_name") != payload["property"]:
            return None, "вопрос о %r не задан (сейчас %r)" % (payload["property"],
                                                              (last.get("next_question") or {}).get("property_name"))
    if question is None:
        question = last.get("next_question")
        if not question:
            return None, "сервис не задал вопрос"
    if question.get("kind") in ("category", "code", "conflict") and not payload.get("value"):
        return None, "вопрос %r требует другого шага, а не ответа значения" % question.get("kind")
    answer = {"question_id": question.get("id")}
    if payload.get("unknown"):
        answer["unknown"] = True
    elif "values" in payload:
        answer["values"] = payload["values"]
    else:
        answer["value"] = payload.get("value", "")
    if payload.get("unit"):
        answer["unit"] = payload["unit"]
    return answer, None


# ----------------------------------------------------------------------- run
def run_case(base: str, case: dict, defaults: dict, verbose: bool, allow_requires: bool = False) -> dict:
    result = {"id": case["id"], "title": case.get("title"), "checks": case.get("checks") or [],
              "source": case.get("source"), "known_issue": bool(case.get("known_issue")),
              "discover": bool(case.get("discover")), "status": "PASS", "notes": [], "steps": [],
              "skip": False}
    if case.get("requires") and not allow_requires:
        result["status"] = "SKIP"
        result["notes"].append("требует окружения: %s (в подготовленном окружении запускать с --all)" % case["requires"])
        return result

    last: dict = {}
    remembered: dict = {}
    payload: dict = {}
    for index, step in enumerate(case.get("steps") or [], 1):
        where = "%s шаг %d" % (case["id"], index)
        if "post" in step:
            payload = merge(step["post"], defaults)
            http, body = post_analysis(base, payload)
        elif "answer" in step:
            answer, error = answer_from_question(step["answer"], remembered, last)
            if error:
                result["status"] = "FAIL"
                result["notes"].append("%s: %s" % (where, error))
                break
            payload = dict(payload)
            payload.setdefault("answers", [])
            payload["answers"] = [a for a in payload["answers"] if a.get("question_id") != answer["question_id"]]
            payload["answers"].append(answer)
            http, body = post_analysis(base, payload)
        elif "confirm_code" in step:
            code = step["confirm_code"]
            if code == "auto":
                code = (last.get("candidates") or [{}])[0].get("ktru_code")
            payload = dict(payload, confirmed_ktru_code=code)
            http, body = post_analysis(base, payload)
        elif "confirm_category" in step:
            code = step["confirm_category"]
            if code == "auto":
                code = (last.get("categories") or [{}])[0].get("id")
            payload = dict(payload, confirmed_category_id=code)
            http, body = post_analysis(base, payload)
        elif "get_position" in step:
            http, body = request(base, "GET", "/api/v1/catalog/positions/%s" % step["get_position"])
        else:
            result["status"] = "FAIL"
            result["notes"].append("%s: неизвестный шаг %s" % (where, sorted(step)))
            break

        last = body if isinstance(body, dict) else {}
        if step.get("capture_question"):
            remembered[step["capture_question"]] = last.get("next_question")
        step_record = {"http": http, "status": last.get("status"), "selected": last.get("selected_ktru_code"),
                       "candidates": last.get("candidate_count"), "reason": classify(last)}
        result["steps"].append(step_record)
        if verbose:
            out = pathlib.Path(".tools/user_cases")
            out.mkdir(parents=True, exist_ok=True)
            (out / ("%s.%d.json" % (case["id"], index))).write_text(
                json.dumps({"request": payload, "http": http, "response": body}, ensure_ascii=False, indent=1),
                encoding="utf-8")
        if http is None:
            result["status"] = "FAIL"
            result["notes"].append("%s: сервис недоступен (%s)" % (where, last.get("transport_error")))
            break
        if step.get("expect"):
            ok, notes = check_expect(last, step["expect"], http, where)
            if not ok:
                if result["status"] != "FAIL":
                    result["status"] = "FAIL"
                result["notes"].extend(notes)
        for problem in invariants(last):
            result["status"] = "FAIL"
            result["notes"].append("%s: инвариант — %s" % (where, problem))
    if result["status"] == "PASS" and case.get("reason"):
        actual = classify(last)
        result["reason_expected"] = case["reason"]
        result["reason_actual"] = actual
        if actual != case["reason"]:
            result["status"] = "NOTE"
            result["notes"].append("причина незавершения %r вместо ожидаемой %r" % (actual, case["reason"]))
    elif result["status"] != "FAIL":
        result["reason_actual"] = classify(last)
    if result["known_issue"]:
        result["status"] = "KNOWN-OPEN" if result["status"] in ("FAIL", "NOTE") else "KNOWN-FIXED"
    return result


# -------------------------------------------------------------------- replay
def as_obj(value):
    """Артефакты ревью хранят часть тел как python-repr-строки, часть как JSON-объекты."""
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in "[{":
            try:
                return ast.literal_eval(text)
            except (ValueError, SyntaxError):
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return None
    return None


def records_from(path: pathlib.Path) -> list:
    """Достаёт пары (метод, путь, запрос, ожидаемый HTTP, ожидаемый ответ) из артефактов ревью."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    items = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("records") or data.get("http") or []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        request_body = as_obj(item.get("request"))
        response = as_obj(item.get("response"))
        if response is None:
            continue
        route = item.get("route") or ""
        method = item.get("method")
        target = item.get("path")
        if route:
            head, _, tail = route.partition(" ")
            if tail:
                method = method or head
                target = target or tail
            else:
                target = target or head
        if not method:
            method = "POST" if request_body is not None else "GET"
        if not target:
            target = "/api/v1/analyses" if method == "POST" else "/api/v1/catalog/status"
        expected_http = item.get("http_status") or item.get("status_code")
        try:
            expected_http = int(expected_http) if expected_http is not None else None
        except (TypeError, ValueError):
            expected_http = None
        out.append({"scenario": item.get("scenario") or route or target, "method": method, "path": target,
                    "request": request_body, "response": response, "http": expected_http})
    return out


def replay(base: str, only: str | None, verbose: bool, limit: int | None) -> dict:
    files = sorted(p for p in REVIEWS.rglob("*.json")
                   if "before" not in p.name and "browser" not in p.name and p.name != "checks.json")
    stats = {"files": 0, "records": 0, "duplicates": 0, "pass": 0, "fail": 0, "changed": 0, "skip": 0,
             "diffs": [], "changed_diffs": []}
    seen = set()
    for path in files:
        records = records_from(path)
        if not records:
            continue
        stats["files"] += 1
        rel = path.relative_to(REVIEWS.parent.parent)
        for record in records:
            if only and only not in record["scenario"] and only not in str(rel):
                continue
            if limit and stats["records"] >= limit:
                break
            key = (record["method"], record["path"], json.dumps(record["request"], sort_keys=True, ensure_ascii=False))
            if key in seen:
                stats["duplicates"] += 1
                continue
            seen.add(key)
            stats["records"] += 1
            http, body = request(base, record["method"], record["path"], record["request"])
            expected = record["response"] if isinstance(record["response"], dict) else {}
            if http is None:
                stats["skip"] += 1
                stats["diffs"].append("%s: сервис недоступен" % rel)
                continue
            if record["http"] and http != record["http"]:
                stats["fail"] += 1
                stats["diffs"].append("%s | %s: HTTP %s вместо %s" % (rel, record["scenario"], http, record["http"]))
                continue
            if record["method"] == "GET":
                body = body if isinstance(body, dict) else {}
                expected_position = (expected.get("position") or {}).get("code") if isinstance(expected, dict) else None
                ok = bool(body)
                detail = ""
                if expected_position and (body.get("position") or {}).get("code") != expected_position:
                    ok = False
                    detail = "позиция %r вместо %r" % ((body.get("position") or {}).get("code"), expected_position)
                # Версия поставки — сведения, а не поведение: записанные прогоны сняты
                # на разных ревизиях clean (5), поэтому расхождение не считаем провалом.
                if expected.get("catalog_version") and body.get("catalog_version") != expected["catalog_version"]:
                    stats.setdefault("version_notes", []).append(
                        "%s | %s: catalog_version %r вместо записанной %r"
                        % (rel, record["scenario"], body.get("catalog_version"), expected["catalog_version"]))
                stats["pass" if ok else "fail"] += 1
                if not ok:
                    stats["diffs"].append("%s | %s: %s" % (rel, record["scenario"], detail or "пустой ответ GET"))
                continue
            if record["http"] and record["http"] >= 400:
                code = ((body or {}).get("detail") or {}).get("code") if isinstance(body, dict) else None
                want = ((expected or {}).get("detail") or {}).get("code")
                ok = code == want
                stats["pass" if ok else "fail"] += 1
                if not ok:
                    stats["diffs"].append("%s | %s: код ошибки %r вместо %r" % (rel, record["scenario"], code, want))
                continue
            fields = [("status", body.get("status"), expected.get("status")),
                      ("selected", body.get("selected_ktru_code"), expected.get("selected_ktru_code")),
                      ("candidates", body.get("candidate_count"), expected.get("candidate_count")),
                      ("excluded", body.get("excluded_count"), expected.get("excluded_count"))]
            for key in VERIFICATION_KEYS:
                fields.append(("verification." + key, (body.get("verification") or {}).get(key),
                               (expected.get("verification") or {}).get(key)))
            question_now, question_then = body.get("next_question") or {}, expected.get("next_question") or {}
            fields.append(("question.kind", question_now.get("kind"), question_then.get("kind")))
            fields.append(("question.property", question_now.get("property_name"), question_then.get("property_name")))
            bad = [(name, now, then) for name, now, then in fields if now != then]
            engine_now, engine_then = body.get("engine_version"), expected.get("engine_version")
            if bad and engine_now and engine_then and engine_now != engine_then:
                # Прогон снят другим движком: поведение сравнимо, но это не регрессия.
                stats["changed"] += 1
                stats["changed_diffs"].append(
                    "%s | %s: движок записан %s, сейчас %s -> %s" % (rel, record["scenario"], engine_then, engine_now,
                                                                     "; ".join("%s=%r вместо %r" % item for item in bad)))
            elif bad:
                stats["fail"] += 1
                stats["diffs"].append("%s | %s: %s" % (rel, record["scenario"],
                                                       "; ".join("%s=%r вместо %r" % item for item in bad)))
            else:
                stats["pass"] += 1
            if verbose:
                out = pathlib.Path(".tools/user_cases/replay")
                out.mkdir(parents=True, exist_ok=True)
                name = "%s.%s.json" % (rel.name, str(record["scenario"])[:40].replace("/", "_"))
                (out / name).write_text(json.dumps({"request": record["request"], "http": http, "response": body},
                                                   ensure_ascii=False, indent=1), encoding="utf-8")
        if limit and stats["records"] >= limit:
            break
    return stats


# ---------------------------------------------------------------------- main
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Пользовательские сценарии и реплей записанных прогонов")
    parser.add_argument("mode", choices=("list", "run", "replay"))
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--only", default=None, help="подстрока id кейса или сценария")
    parser.add_argument("--verbose", action="store_true", help="складывать ответы в .tools/user_cases/")
    parser.add_argument("--all", action="store_true", help="включать кейсы, требующие особого окружения")
    parser.add_argument("--limit", type=int, default=None, help="ограничить число записей реплея")
    parser.add_argument("--json", dest="json_path", default=None)
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    document = json.loads(CASES.read_text(encoding="utf-8"))
    defaults = document.get("defaults") or {}
    cases = [case for case in document["cases"] if args.all or not case.get("requires")]

    if args.mode == "list":
        print("Пользовательские сценарии: %d" % len(cases))
        for case in cases:
            mark = " (known issue)" if case.get("known_issue") else ""
            discover = " [discover]" if case.get("discover") else ""
            print("  %-32s %-10s %s%s%s" % (case["id"], case.get("kind"), case.get("title"), mark, discover))
        print("\nПроверки: %s" % ", ".join(sorted({check.split(":")[0] for case in cases for check in case.get("checks") or []})))
        return 0

    if args.mode == "replay":
        stats = replay(args.base, args.only, args.verbose, args.limit)
        print("Реплей записанных прогонов: файлов %d, уникальных запросов %d (повторов пропущено %d), "
              "совпало %d, расхождений %d, на другом движке %d, недоступно %d"
              % (stats["files"], stats["records"], stats.get("duplicates", 0), stats["pass"], stats["fail"],
                 stats.get("changed", 0), stats["skip"]))
        for line in stats.get("version_notes") or []:
            print("  версия поставки (сведения) %s" % line)
        for line in (stats.get("changed_diffs") or [])[:20]:
            print("  ДРУГОЙ ДВИЖОК %s" % line)
        for line in (stats.get("changed_diffs") or [])[20:]:
            print("  ДРУГОЙ ДВИЖОК ... %s" % line)
        for line in stats["diffs"][:40]:
            print("  РЕГРЕССИЯ %s" % line)
        if len(stats["diffs"]) > 40:
            print("  ... ещё %d" % (len(stats["diffs"]) - 40))
        if args.json_path:
            write_json(args.json_path, stats)
        return 1 if stats["fail"] else 0

    results = []
    for case in cases:
        if args.only and args.only not in case["id"]:
            continue
        results.append(run_case(args.base, case, defaults, args.verbose, allow_requires=args.all))

    order = {"FAIL": 0, "NOTE": 1, "KNOWN-OPEN": 2, "KNOWN-FIXED": 3, "SKIP": 4, "PASS": 5}
    for result in sorted(results, key=lambda item: order.get(item["status"], 9)):
        print("%-12s %-32s %s" % (result["status"], result["id"], result["title"]))
        if result.get("reason_actual"):
            print("             причина: %s%s" % (result["reason_actual"],
                  " (ожидалась %s)" % result.get("reason_expected") if result.get("reason_expected") else ""))
        if result["steps"]:
            print("             шаги: " + " | ".join(
                "http=%s status=%s cand=%s sel=%s" % (step["http"], step["status"], step["candidates"], step["selected"])
                for step in result["steps"]))
        for note in result["notes"]:
            print("             - %s" % note)

    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    noted = sum(1 for r in results if r["status"] == "NOTE")
    known = sum(1 for r in results if r["status"] == "KNOWN-OPEN")
    fixed = sum(1 for r in results if r["status"] == "KNOWN-FIXED")
    skipped = sum(1 for r in results if r["status"] == "SKIP")
    print("\nИтог: пройдено %d, провалено %d, замечаний %d, известных дефектов %d, закрытых дефектов %d, пропущено %d"
          % (passed, failed, noted, known, fixed, skipped))
    reasons = {}
    for result in results:
        if result.get("reason_actual"):
            reasons[result["reason_actual"]] = reasons.get(result["reason_actual"], 0) + 1
    if reasons:
        print("Причины незавершения: " + ", ".join("%s=%d" % item for item in sorted(reasons.items())))
    if args.json_path:
        write_json(args.json_path, {"results": results})
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
