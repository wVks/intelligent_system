#!/usr/bin/env python3
"""Offline application pilot: real-MCP HTTP cases plus isolated arithmetic/agent guards."""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import importlib.metadata
import json
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from studynavigator.agent import AgentError, StudyAgent
from studynavigator.config import Settings
from studynavigator.main import create_app
from studynavigator.mcp_gateway import ARGUMENT_MODELS, GatewayError, MCPGateway
from studynavigator.planner import build_plan


def same_value(left, right):
    return json.dumps(left, ensure_ascii=False, sort_keys=True) == json.dumps(right, ensure_ascii=False, sort_keys=True)


def check(name, passed, evidence):
    return {"name": name, "passed": bool(passed), "evidence": evidence}


class ScriptedProvider:
    mode = "offline"
    model = None

    def __init__(self, turns):
        self.turns, self.index = turns, 0

    async def complete(self, messages, tools, *, require_tool=False):
        turn = self.turns[min(self.index, len(self.turns) - 1)]
        self.index += 1
        return copy.deepcopy(turn)


class GuardGateway:
    """Synthetic evidence for guard tests; deliberately not called real MCP."""

    validate_arguments = staticmethod(MCPGateway.validate_arguments)

    def __init__(self, dataset, *, instruction=None, error=None):
        self.assignment = copy.deepcopy(dataset["assignment"])
        self.assignment["sources"] = copy.deepcopy(dataset["sources"])
        self.assignment["source_ids"] = [source["source_id"] for source in dataset["sources"]]
        if instruction:
            self.assignment["sources"][0]["excerpt"] += "\n" + instruction
        self.tools = [{"name": name, "description": "Synthetic guard fixture",
                       "inputSchema": schema.model_json_schema()}
                      for name, schema in ARGUMENT_MODELS.items()]
        self.dispatches, self.trace, self.error = [], [], error

    async def call(self, name, arguments):
        validated = self.validate_arguments(name, arguments)
        self.dispatches.append((name, validated))
        self.trace.append({"name": name, "status": self.error.code if self.error else "ok", "duration_ms": 0})
        if self.error:
            raise self.error
        if name == "get_assignment":
            return {"assignment": copy.deepcopy(self.assignment)}
        if name == "list_assignments":
            return {"assignments": [copy.deepcopy(self.assignment)]}
        return {"matches": copy.deepcopy(self.assignment["sources"])}


def call_turn(name="get_assignment", arguments=None):
    if arguments is None:
        arguments = {"assignment_id": "personal-project"}
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": "guard-call", "type": "function", "function": {
            "name": name, "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments)}}]}


def final_turn(facts=None):
    if facts is None:
        facts = [{"key": "lms_links_count", "value": 2, "source_id": "personal-project-p1"}]
    return {"role": "assistant", "content": json.dumps({
        "answer": "Синтетический ответ для проверки защитного валидатора.", "status": "answered",
        "source_ids": sorted({fact["source_id"] for fact in facts}), "facts": facts}, ensure_ascii=False)}


async def evaluate_guard(case, seed):
    scenario = case["input"]["scenario"]
    error = GatewayError("transport_unavailable", "Синтетический сбой источника") if scenario == "tool_error" else None
    gateway = GuardGateway(seed, instruction=case["input"].get("material_instruction"), error=error)
    turns = {
        "unknown_tool": [call_turn("delete_assignments")],
        "malformed_arguments": [call_turn(arguments="{broken")],
        "tool_error": [call_turn(), final_turn()],
        "tool_loop": [call_turn()],
        "forged_deadline": [call_turn(), final_turn([
            {"key": "due_date", "value": "2026-11-30", "source_id": "personal-project-p1"}])],
        "forged_source": [call_turn(), final_turn([
            {"key": "lms_links_count", "value": 2, "source_id": "personal-project-p3"}])],
        "no_tool_evidence": [final_turn()],
        "write_tool": [call_turn(), call_turn("delete_assignments")],
    }[scenario]
    expected_codes = {
        "unknown_tool": {"unknown_tool"}, "malformed_arguments": {"invalid_tool_arguments"},
        "tool_error": {"transport_unavailable"}, "tool_loop": {"tool_limit_exceeded", "context_limit_exceeded"},
        "forged_deadline": {"unsupported_fact"}, "forged_source": {"unsupported_fact"},
        "no_tool_evidence": {"assignment_context_required"},
        "write_tool": {"unknown_tool"},
    }[scenario]
    observed = None
    response = None
    try:
        response = await StudyAgent(gateway, ScriptedProvider(turns), max_calls=6, request_timeout=2).run("Проверь требования задания")
    except AgentError as exc:
        observed = {"code": exc.code, "message": exc.message}
    checks = [check("controlled_rejection", observed is not None and observed["code"] in expected_codes,
                    f"Expected controlled code {sorted(expected_codes)}; received {observed}")]
    if case["expected"].get("no_dispatch"):
        checks.append(check("no_dispatch", not gateway.dispatches, f"Dispatched calls: {gateway.dispatches}"))
    checks.append(check("bounded_calls", len(gateway.dispatches) <= case["expected"]["calls_lte"],
                        f"Actual calls: {len(gateway.dispatches)}"))
    if case["expected"].get("no_illegal_dispatch"):
        checks.append(check("no_illegal_dispatch", all(name in ARGUMENT_MODELS for name, _ in gateway.dispatches),
                            f"Dispatched names: {[name for name, _ in gateway.dispatches]}"))
    return checks, {"execution": "synthetic_agent_guard", "error": observed,
                    "response": response, "trace": gateway.trace}


def evaluate_plan(case):
    expected = case["expected"]
    try:
        result = build_plan(**case["input"])
    except ValueError as exc:
        return [check("invalid_budget_rejected", expected.get("reject") is True, str(exc))], {"execution": "deterministic_plan", "error": str(exc)}
    if expected.get("reject"):
        return [check("invalid_budget_rejected", False, "Planner accepted invalid input")], {"response": result}
    checks = [check("plan_status", result["status"] == expected["status"],
                    f"Expected {expected['status']}; got {result['status']}")]
    for key in ["required_minutes", "available_minutes", "deficit_minutes"]:
        if key in expected:
            checks.append(check(key, result[key] == expected[key], f"Expected {expected[key]}; got {result[key]}"))
    totals = [sum(task["minutes"] for task in day["tasks"]) for day in result["schedule"]]
    checks.append(check("daily_capacity", all(value <= case["input"]["minutes_per_day"] for value in totals),
                        f"Scheduled per day: {totals}"))
    if expected.get("undated"):
        checks.append(check("no_invented_date", all(day["date"] is None for day in result["schedule"]),
                            "Relative plans keep dates null"))
    return checks, {"execution": "deterministic_plan", "response": result}


def evaluate_chat(case, client, canonical_sources, canonical_facts):
    response = client.post("/api/chat", json=case["input"])
    result = response.json()
    checks = [check("http_success", response.status_code == 200, f"HTTP {response.status_code}")]
    if response.status_code != 200:
        return checks, {"execution": "http_actual_mcp", "response": result}
    expected = case["expected"]
    checks.append(check("explicit_offline_mode", result.get("mode") == "offline", f"Mode: {result.get('mode')}"))
    checks.append(check("answer_status", result.get("status") == expected["status"],
                        f"Expected {expected['status']}; got {result.get('status')}"))
    trace = result.get("trace", [])
    checks.append(check("mcp_evidence", any(t.get("name") == "get_assignment" and t.get("status") == "ok" for t in trace),
                        f"Tool trace: {trace}"))
    facts = result.get("facts", [])
    if "fact" in expected:
        target = expected["fact"]
        checks.append(check("typed_expected_fact", any(same_value(fact, target) for fact in facts),
                            f"Expected {target}; received {facts}"))
    canonical = all(fact.get("key") in canonical_facts and same_value(fact, canonical_facts[fact["key"]]) for fact in facts)
    checks.append(check("all_facts_supported", canonical, f"Received facts: {facts}"))
    sources = result.get("sources", [])
    exact_sources = all(source.get("source_id") in canonical_sources and all(
        same_value(source.get(field), canonical_sources[source["source_id"]].get(field))
        for field in ["page", "excerpt", "url"]) for source in sources)
    checks.append(check("source_provenance", exact_sources, f"Source IDs: {[s.get('source_id') for s in sources]}"))
    if expected.get("no_fabricated_facts"):
        checks.append(check("no_fabricated_facts", not facts, f"Unknown question facts: {facts}"))
    if "source" in expected:
        target = expected["source"]
        matches = [source for source in sources if source.get("source_id") == target["source_id"]]
        source_ok = bool(matches) and matches[0].get("page") == target["page"] and bool(matches[0].get("excerpt"))
        checks.append(check("expected_supporting_source", source_ok, f"Expected {target}; received source IDs {[s.get('source_id') for s in sources]}"))
        if expected.get("source_resolves") and matches:
            opened = client.get(matches[0]["url"])
            checks.append(check("source_opens", opened.status_code == 200 and f"#page={target['page']}" in opened.text,
                                f"Source URL HTTP {opened.status_code}; requested page {target['page']}"))
    return checks, {"execution": "http_actual_mcp", "response": result}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=ROOT / "evals/app/cases.json")
    parser.add_argument("--output", type=Path, default=ROOT / "var/eval-offline.json")
    parser.add_argument("--split", choices=["all", "dev", "holdout"], default="all")
    args = parser.parse_args(argv)
    try:
        dataset_bytes = args.cases.read_bytes()
        dataset = json.loads(dataset_bytes)
        all_cases = dataset["cases"]
        if not isinstance(dataset.get("dataset_id"), str) or not isinstance(all_cases, list):
            raise ValueError("Dataset requires dataset_id and cases list")
        if any(not isinstance(case, dict) or not isinstance(case.get("id"), str)
               or not case["id"].strip() or type(case.get("critical")) is not bool
               or case.get("split") not in {"dev", "holdout"}
               or case.get("kind") not in {"chat", "plan", "guard"}
               or not isinstance(case.get("expected"), dict) or not isinstance(case.get("input"), dict)
               for case in all_cases):
            raise ValueError("Invalid case identity, split, kind, critical flag or input/expected structure")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    ids = [case["id"] for case in all_cases]
    if not ids or len(ids) != len(set(ids)):
        parser.error("Cases must have unique nonempty IDs")
    cases = [case for case in all_cases if args.split == "all" or case["split"] == args.split]
    if not cases:
        parser.error("The selected split contains no cases")
    seed = json.loads((ROOT / "data/course.json").read_text())
    canonical_sources = {source["source_id"]: source for source in seed["sources"]}
    canonical_facts = {fact["key"]: fact for fact in seed["assignment"]["facts"]}
    records = []
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="study-eval-") as temporary:
        settings = Settings(database_path=Path(temporary) / "study.sqlite", mode="offline", rate_limit_per_minute=1000)
        with TestClient(create_app(settings=settings)) as client:
            for case in cases:
                case_started = time.monotonic()
                try:
                    if case["kind"] == "chat":
                        checks, artifacts = evaluate_chat(case, client, canonical_sources, canonical_facts)
                    elif case["kind"] == "plan":
                        checks, artifacts = evaluate_plan(case)
                    elif case["kind"] == "guard":
                        checks, artifacts = asyncio.run(evaluate_guard(case, seed))
                    else:
                        raise ValueError("Unknown case kind")
                except Exception as exc:
                    checks, artifacts = [check("case_execution", False, f"{type(exc).__name__}: {exc}")], {"error": type(exc).__name__}
                record = {"id": case["id"], "category": case["category"], "split": case["split"],
                          "critical": case["critical"], "input": case["input"],
                          "passed": all(item["passed"] for item in checks), "checks": checks,
                          "elapsed_ms": round((time.monotonic() - case_started) * 1000, 2), **artifacts}
                records.append(record)
                print(("PASS" if record["passed"] else "FAIL") + " " + case["id"])
    passed = sum(record["passed"] for record in records)
    grade_checks = [check for record in records for check in record["checks"]]
    categories = {}
    for category in sorted({record["category"] for record in records}):
        members = [record for record in records if record["category"] == category]
        categories[category] = {"passed": sum(record["passed"] for record in members), "total": len(members)}
    source_digest = hashlib.sha256()
    for path in sorted((ROOT / "studynavigator").rglob("*.py")):
        source_digest.update(path.relative_to(ROOT).as_posix().encode())
        source_digest.update(path.read_bytes())
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True))
    except (OSError, subprocess.CalledProcessError):
        commit, dirty = None, None
    report = {
        "dataset_id": dataset["dataset_id"], "dataset_sha256": hashlib.sha256(dataset_bytes).hexdigest(),
        "created_at": datetime.now(timezone.utc).isoformat(), "mode": "offline", "split": args.split,
        "evaluator_version": 2,
        "app_source_sha256": source_digest.hexdigest(),
        "course_sha256": hashlib.sha256((ROOT / "data/course.json").read_bytes()).hexdigest(),
        "git_commit": commit, "working_tree_dirty": dirty,
        "configuration": {"mode": "offline", "model": None, "max_calls": 6,
                          "request_timeout_seconds": 45, "rate_limit_per_minute": 1000,
                          "rate_limit_override_for_isolated_eval": True},
        "dependencies": {name: importlib.metadata.version(name) for name in ["mcp", "fastapi", "pydantic", "httpx"]},
        "case_summary": {"passed": passed, "total": len(cases), "pass_rate": passed / len(cases)},
        "check_summary": {"passed": sum(item["passed"] for item in grade_checks), "total": len(grade_checks)},
        "all_critical_passed": all(record["passed"] for record in records if record["critical"]),
        "categories": categories, "elapsed_seconds": round(time.monotonic() - started, 2),
        "limitations": [
            "No live LLM or hosting evaluated. Normal chat uses deterministic offline provider and actual MCP stdio.",
            "Synthetic guard cases measure validation behavior, not model resistance to prompt injection.",
            "Typed facts/provenance are checked; free-text semantic completeness requires independent review.",
            "Pilot cases are visible to developers; holdout labels do not establish an independent blinded evaluation.",
        ],
        "cases": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Offline cases: {passed}/{len(cases)}; report: {args.output}")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
