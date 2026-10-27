#!/usr/bin/env python3
"""Hosted HTTP smoke. Default flow makes no LLM requests and deletes its own fixture.

--live explicitly adds one hosted /api/chat request, which may cause multiple
paid provider calls. Provider spending limits must be configured on the server.
No API key is read by this script.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit, urlunsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.run_live_eval import pdf_fixture

CHECKS = (
    "verified_https", "http_redirects_https", "health", "index", "javascript", "stylesheet", "defense_html",
    "mcp_discovery", "session_cookie", "private_cache", "invalid_pdf_rejected", "upload", "source_text",
    "original_pdf", "source_html_page", "foreign_assignment_404", "foreign_source_404", "foreign_pdf_404",
    "manual_confirmation", "plan_deficit_180", "all_completed_zero", "stale_plan_rejected", "cross_origin_rejected",
    "delete_fixture", "deleted_assignment_404", "deleted_source_404", "deleted_pdf_404",
)


def json_data(response):
    if response is None:
        return {}
    try:
        value = response.json()
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def http_status(response):
    return response.status_code if response is not None else None


def redact(value):
    if isinstance(value, str):
        return re.sub(r"gpt://[^/\s]+/", "gpt://<folder_id>/", value)
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def check_hosted(base_url, *, live=False, timeout=30, skip_http_redirect=False, client_factory=None, progress=None):
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Provide an HTTP(S) base URL without credentials, query or fragment")
    base_url = base_url.rstrip("/")
    factory = client_factory or (lambda url: httpx.Client(base_url=url, timeout=timeout, follow_redirects=True))
    expected = [name for name in CHECKS if name != "http_redirects_https" or not skip_http_redirect]
    if live:
        expected.append("live_chat")
    report = {"base_url": base_url, "started_at": datetime.now(timezone.utc).isoformat(), "timezone": "Europe/Moscow",
              "mode": "hosted_with_explicit_live_chat" if live else "hosted_no_llm",
              "live_requested": live, "live_http_chat_requests": 0, "checks": [],
              "limitations": ["HTTP smoke is not a browser/keyboard audit.",
                              "Manual card confirmation tests API/storage/planning and is not an AI document analysis.",
                              "No restart or persistence-after-restart claim follows from this single run.",
                              "No server API key or session cookie value is recorded."]}

    def record(name, passed, evidence):
        item = {"name": name, "passed": bool(passed), "evidence": evidence}
        report["checks"].append(item)
        if progress:
            progress(item)

    def request(client, method, path, **kwargs):
        try:
            return client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            report.setdefault("transport_errors", []).append({"path": path, "kind": type(exc).__name__})
            return None

    assignment = source = document = None
    with factory(base_url) as client, factory(base_url) as foreign:
        try:
            # httpx verifies certificates by default; there is no insecure switch.
            record("verified_https", parsed.scheme == "https", "HTTPS URL with default certificate verification; no verify=False option.")
            if not skip_http_redirect:
                http_url = urlunsplit(("http", parsed.netloc, parsed.path or "/", "", ""))
                response = request(client, "GET", http_url, follow_redirects=False)
                location = response.headers.get("location", "") if response is not None else ""
                redirected = urlsplit(location)
                record("http_redirects_https", http_status(response) in {301, 302, 307, 308}
                       and redirected.scheme == "https" and redirected.hostname == parsed.hostname,
                       f"HTTP {http_status(response)}; redirect keeps the same host and uses HTTPS.")
            response = request(client, "GET", "/health")
            data = json_data(response)
            record("health", http_status(response) == 200 and data.get("status") == "ok", f"HTTP {http_status(response)}; status={data.get('status')}")
            for name, path, marker in (("index", "/", "StudyNavigator"), ("javascript", "/static/app.js", "getElementById"),
                                       ("stylesheet", "/static/styles.css", ":root"), ("defense_html", "/static/defense.html", "slide-8")):
                response = request(client, "GET", path)
                record(name, http_status(response) == 200 and marker in response.text if response is not None else False,
                       f"{path}: HTTP {http_status(response)}; expected artifact marker.")
            response = request(client, "GET", "/api/status")
            status = json_data(response)
            report["application_status"] = redact(status)
            record("mcp_discovery", http_status(response) == 200 and status.get("mcp_tools") == 3 and status.get("transport") == "stdio",
                   f"HTTP {http_status(response)}; tools={status.get('mcp_tools')}; transport={status.get('transport')}")
            # Cookie values are never copied into the report.
            response = request(client, "GET", "/api/assignments")
            cookies = list(client.cookies.jar)
            session = next((cookie for cookie in cookies if cookie.name == "study_session"), None)
            attributes = {key.lower(): value for key, value in getattr(session, "_rest", {}).items()}
            record("session_cookie", session is not None and session.secure and "httponly" in attributes
                   and str(attributes.get("samesite", "")).lower() == "lax",
                   "Session cookie exists; Secure, HttpOnly and SameSite=Lax checked without recording its value.")
            headers = response.headers if response is not None else {}
            record("private_cache", http_status(response) == 200 and "no-store" in headers.get("cache-control", "")
                   and "cookie" in headers.get("vary", "").lower(), "Private API response is no-store and varies by Cookie.")
            response = request(client, "POST", "/api/assignments", data={"title": "Invalid hosted smoke PDF"},
                               files=[("files", ("broken.pdf", b"%PDF-1.7\nbroken fixture", "application/pdf"))])
            record("invalid_pdf_rejected", http_status(response) == 422 and json_data(response).get("error", {}).get("code") == "invalid_pdf",
                   f"Malformed fixture HTTP {http_status(response)}; no analysis request sent.")
            response = request(client, "POST", "/api/assignments", data={"title": "Temporary hosted smoke fixture"},
                               files=[("files", ("hosted-smoke.pdf", pdf_fixture("Hosted smoke assignment. Goal: compare two datasets. Submit a short PDF report. Requirement: include one chart."), "application/pdf"))])
            assignment = json_data(response).get("assignment")
            record("upload", http_status(response) == 201 and bool(assignment), f"Text PDF upload HTTP {http_status(response)}.")
            if not assignment:
                raise RuntimeError("fixture_upload_failed")
            source, document = assignment["sources"][0], assignment["documents"][0]
            response = request(client, "GET", f"/api/assignments/{assignment['assignment_id']}/materials", params={"q": "chart"})
            matches = json_data(response).get("matches", [])
            record("source_text", http_status(response) == 200 and any("chart" in item.get("excerpt", "") for item in matches),
                   "Search for chart returned a fragment from the uploaded fixture through real MCP.")
            response = request(client, "GET", document["url"])
            record("original_pdf", http_status(response) == 200 and response.content.startswith(b"%PDF-"), "Uploaded original PDF is readable.")
            response = request(client, "GET", source["url"])
            record("source_html_page", http_status(response) == 200 and f"{document['url']}#page={source['page']}" in response.text,
                   "Source page links to this original PDF and the correct page.")
            for name, path in (("foreign_assignment_404", f"/api/assignments/{assignment['assignment_id']}"),
                               ("foreign_source_404", f"/api/sources/{source['source_id']}"), ("foreign_pdf_404", document["url"])):
                response = request(foreign, "GET", path)
                record(name, http_status(response) == 404, f"Different session HTTP {http_status(response)}.")
            field = lambda text: {"text": text, "origin": "user", "source_ids": []}
            card = {"title": "Temporary hosted smoke fixture", "goal": field("Compare two datasets"),
                    "deliverable": field("Short PDF report"), "requirements": [field("Include one chart")],
                    "criteria": [], "deadline": {"date": None, "origin": "unknown", "source_ids": []},
                    "tasks": [{"id": "task-1", "title": "Compare data", "minutes": 300, "estimate_source": "user", "source_ids": []},
                              {"id": "task-2", "title": "Check report", "minutes": 60, "estimate_source": "user", "source_ids": []}], "questions": []}
            response = request(client, "POST", f"/api/assignments/{assignment['assignment_id']}/confirm", json={"card": card, "revision": assignment["card_revision"]})
            confirmed = json_data(response).get("assignment", {})
            record("manual_confirmation", http_status(response) == 200 and confirmed.get("status") == "ready"
                   and confirmed.get("card", {}).get("goal", {}).get("origin") == "user", "Explicit manual input confirmed, without /analyze or LLM.")
            if not confirmed:
                raise RuntimeError("fixture_confirmation_failed")
            tasks = [{**task, "completed": index > 0} for index, task in enumerate(confirmed["card"]["tasks"])]
            payload = {"assignment_id": assignment["assignment_id"], "card_revision": confirmed["card_revision"], "days": 1, "minutes_per_day": 120, "tasks": tasks}
            response = request(client, "POST", "/api/plan", json=payload)
            plan = json_data(response)
            record("plan_deficit_180", http_status(response) == 200 and plan.get("required_minutes") == 300 and plan.get("deficit_minutes") == 180,
                   "300 minutes remaining, 120 available: expected deficit 180.")
            for task in tasks:
                task["completed"] = True
            response = request(client, "POST", "/api/plan", json=payload)
            plan = json_data(response)
            record("all_completed_zero", http_status(response) == 200 and plan.get("required_minutes") == 0 and plan.get("schedule") == [],
                   "All tasks completed: zero work and empty schedule.")
            response = request(client, "POST", "/api/plan", json={**payload, "card_revision": assignment["card_revision"]})
            record("stale_plan_rejected", http_status(response) == 409, f"Old card revision HTTP {http_status(response)}.")
            response = request(client, "POST", "/api/plan", json=payload, headers={"Origin": "https://foreign.invalid"})
            record("cross_origin_rejected", http_status(response) == 403, f"Foreign Origin mutation HTTP {http_status(response)}.")
            if live:
                report["live_http_chat_requests"] += 1
                response = request(client, "POST", "/api/chat", json={"assignment_id": "personal-project", "message": "Когда сдавать проект?"})
                answer = json_data(response)
                report["live_chat"] = redact(answer)
                record("live_chat", http_status(response) == 200 and answer.get("mode") == "live"
                       and answer.get("status") == "insufficient_data" and any(item.get("name") == "get_assignment" for item in answer.get("trace", [])),
                       "Explicit paid hosted chat: unknown deadline, live provider and real MCP trace.")
        except Exception as exc:
            report["flow_error"] = {"kind": type(exc).__name__}
        finally:
            if assignment:
                response = request(client, "DELETE", f"/api/assignments/{assignment['assignment_id']}")
                record("delete_fixture", http_status(response) == 204, f"Only this run's fixture deleted: HTTP {http_status(response)}.")
                for name, path in (("deleted_assignment_404", f"/api/assignments/{assignment['assignment_id']}"),
                                   ("deleted_source_404", f"/api/sources/{source['source_id']}" if source else None),
                                   ("deleted_pdf_404", document["url"] if document else None)):
                    response = request(client, "GET", path) if path else None
                    record(name, http_status(response) == 404, f"Deleted fixture resource HTTP {http_status(response)}.")
    completed = {item["name"] for item in report["checks"]}
    report["not_run"] = [name for name in expected if name not in completed]
    report["passed"] = sum(item["passed"] for item in report["checks"])
    report["total"] = len(expected)
    report["overall_pass"] = not report["not_run"] and not report.get("flow_error") and all(item["passed"] for item in report["checks"])
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url")
    parser.add_argument("--output", type=Path, default=ROOT / "var/hosted-smoke.json")
    parser.add_argument("--live", action="store_true", help="Explicitly add one hosted /api/chat request; can spend server API funds")
    parser.add_argument("--skip-http-redirect", action="store_true", help="Skip HTTP→HTTPS check for a local fixture environment")
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    report = check_hosted(args.base_url, live=args.live, timeout=args.timeout, skip_http_redirect=args.skip_http_redirect,
                          progress=lambda item: print(f"{'PASS' if item['passed'] else 'FAIL'} {item['name']}"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{report['passed']}/{report['total']}; live chat requests={report['live_http_chat_requests']}; report={args.output}")
    return 0 if report["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
