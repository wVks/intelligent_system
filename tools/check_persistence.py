#!/usr/bin/env python3
"""Read-only hosted fixture check across a restart; never calls the model."""
import argparse
import hashlib
import json
from pathlib import Path

import httpx


def snapshot(paths):
    records = []
    for path in paths:
        session = json.loads(path.read_text())
        with httpx.Client(base_url=session["base_url"], timeout=20) as client:
            for cookie in session["cookies"]:
                client.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"])
            for fixture in session["fixtures"]:
                response = client.get("/api/assignments/" + fixture["assignment_id"])
                response.raise_for_status()
                assignment = response.json()["assignment"]
                assert assignment["card_revision"] == fixture["card_revision"]
                assert assignment["status"] == fixture["status"]
                assert assignment["title"] == fixture["title"]
                for document in fixture["documents"]:
                    pdf = client.get(document["url"])
                    pdf.raise_for_status()
                    assert hashlib.sha256(pdf.content).hexdigest() == document["sha256"]
                for source_id in fixture["source_ids"]:
                    assert client.get("/api/sources/" + source_id).status_code == 200
                records.append({"assignment_id": assignment["assignment_id"],
                                "revision": assignment["card_revision"], "status": assignment["status"],
                                "card_sha256": hashlib.sha256(json.dumps(assignment["card"], sort_keys=True,
                                                                        ensure_ascii=False).encode()).hexdigest(),
                                "pdf_sha256": [d["sha256"] for d in fixture["documents"]]})
    return {"paid_calls": 0, "fixtures": records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compare", type=Path)
    args = parser.parse_args()
    result = snapshot(args.session)
    if args.compare:
        result["matches_before"] = result["fixtures"] == json.loads(args.compare.read_text())["fixtures"]
        assert result["matches_before"], "Persisted fixtures changed"
    result["passed"] = True
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(f"PASS: {len(result['fixtures'])} persisted fixtures, cards, sources and PDF hashes; no model calls")


if __name__ == "__main__":
    main()
