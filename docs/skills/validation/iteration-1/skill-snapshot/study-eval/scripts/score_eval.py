#!/usr/bin/env python3
"""Aggregate independently judged eval checks against a fixed manifest (stdlib)."""

import argparse
import json
import sys
from pathlib import Path


def require(condition, message):
    if not condition:
        raise ValueError(message)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def index_rows(rows, label):
    require(isinstance(rows, list), f"{label}: expected a list")
    indexed = {}
    for row in rows:
        require(isinstance(row, dict), f"{label}: each row must be an object")
        case_id = row.get("id")
        require(nonempty(case_id), f"{label}: id must be a nonempty string")
        require(case_id not in indexed, f"{label}: duplicate id {case_id}")
        indexed[case_id] = row
    return indexed


def score(dataset, results):
    require(isinstance(dataset, dict) and isinstance(results, dict),
            "dataset and results must be objects")
    dataset_id = dataset.get("dataset_id")
    require(nonempty(dataset_id), "dataset_id is required")
    require(results.get("dataset_id") == dataset_id, "dataset_id mismatch")
    require(nonempty(results.get("run_id")), "run_id is required")
    require(results.get("mode") in {"offline", "mcp", "live", "hosted"},
            "mode must be offline, mcp, live or hosted")
    expected = index_rows(dataset.get("cases"), "dataset.cases")
    actual = index_rows(results.get("cases"), "results.cases")
    require(bool(expected), "dataset must contain at least one case")
    require(not (set(actual) - set(expected)), "results contain unknown case IDs")

    case_reports = []
    for case_id, case in expected.items():
        names = case.get("checks")
        require(isinstance(names, list) and bool(names), f"{case_id}: checks must not be empty")
        require(all(nonempty(name) for name in names), f"{case_id}: invalid check name")
        require(len(names) == len(set(names)), f"{case_id}: duplicate expected checks")
        critical = case.get("critical", True)
        require(type(critical) is bool, f"{case_id}: critical must be boolean")
        row = actual.get(case_id)
        seen = {}
        error = "missing result" if row is None else row.get("error")
        require(error is None or nonempty(error), f"{case_id}: error must be null or nonempty text")
        if row is not None:
            checks = row.get("checks")
            require(isinstance(checks, list), f"{case_id}: checks must be a list")
            for check in checks:
                require(isinstance(check, dict), f"{case_id}: check must be an object")
                name = check.get("name")
                require(nonempty(name), f"{case_id}: invalid check name")
                require(name in names, f"{case_id}: unknown check {name}")
                require(name not in seen, f"{case_id}: duplicate check {name}")
                require(type(check.get("passed")) is bool, f"{case_id}/{name}: passed must be boolean")
                require(nonempty(check.get("evidence")), f"{case_id}/{name}: evidence is required")
                seen[name] = check

        evaluated = []
        for name in names:
            check = seen.get(name)
            passed = error is None and check is not None and check["passed"]
            evidence = error or (check["evidence"] if check else "missing check")
            evaluated.append({"name": name, "passed": passed, "evidence": evidence})
        case_reports.append({"id": case_id, "critical": critical, "error": error,
                             "passed": all(c["passed"] for c in evaluated), "checks": evaluated})

    all_checks = [check for case in case_reports for check in case["checks"]]
    passed_checks = sum(check["passed"] for check in all_checks)
    passed_cases = sum(case["passed"] for case in case_reports)
    return {
        "dataset_id": dataset_id, "run_id": results["run_id"], "mode": results["mode"],
        "check_summary": {"passed": passed_checks, "total": len(all_checks),
                          "pass_rate": passed_checks / len(all_checks)},
        "case_summary": {"passed": passed_cases, "total": len(case_reports),
                         "pass_rate": passed_cases / len(case_reports)},
        "all_critical_passed": all(c["passed"] for c in case_reports if c["critical"]),
        "cases": case_reports,
        "limitation": "Aggregates supplied judgments; does not verify answer truth or execution mode."
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("results", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = score(json.loads(args.dataset.read_text(encoding="utf-8")),
                       json.loads(args.results.read_text(encoding="utf-8")))
        text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
        else:
            print(text, end="")
        return 0 if report["check_summary"]["passed"] == report["check_summary"]["total"] else 1
    except (ValueError, OSError, TypeError) as exc:
        print(f"Invalid eval input: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
