#!/usr/bin/env python3
"""Validate this project's deliberately small YAML subset and resource links."""

import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]


def validate(skill, project_root=ROOT):
    errors = []
    path = skill / "SKILL.md"
    if not path.is_file():
        return [f"{skill.name}: missing SKILL.md"]
    text = path.read_text(encoding="utf-8")
    match = re.match(r"\A---\n(.*?)\n---\n(.+)\Z", text, re.S)
    if not match:
        return [f"{skill.name}: invalid frontmatter or empty body"]
    metadata = {}
    for line in match[1].splitlines():
        key, separator, value = line.partition(": ")
        if not separator or key not in {"name", "description"} or key in metadata:
            errors.append(f"{skill.name}: unsupported or duplicate metadata: {line}")
            continue
        try:
            metadata[key] = json.loads(value) if value.startswith('"') else value
        except ValueError:
            errors.append(f"{skill.name}: invalid quoted metadata: {key}")
    name = metadata.get("name", "")
    description = metadata.get("description", "")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64 or name != skill.name:
        errors.append(f"{skill.name}: invalid or mismatched name")
    if not isinstance(description, str) or not 1 <= len(description) <= 1024 or any(c in description for c in "<>"):
        errors.append(f"{skill.name}: invalid description")
    if len(text.splitlines()) > 500 or "[TODO:" in text:
        errors.append(f"{skill.name}: oversized or unfinished instructions")

    for document in [path, *sorted((skill / "references").glob("*.md"))]:
        for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", document.read_text(encoding="utf-8")):
            target = target.strip("<>").split("#", 1)[0]
            if not target or urlparse(target).scheme:
                continue
            resolved = (document.parent / unquote(target)).resolve()
            if not resolved.is_relative_to(project_root.resolve()) or not resolved.exists():
                errors.append(f"{document.relative_to(project_root)}: broken/escaping link {target}")

    ui_path = skill / "agents" / "openai.yaml"
    if not ui_path.is_file():
        errors.append(f"{skill.name}: missing UI metadata")
    else:
        ui = {}
        interface_count = 0
        for line in ui_path.read_text(encoding="utf-8").splitlines():
            if line == "interface:":
                interface_count += 1
                continue
            if not line.strip():
                continue
            found = re.fullmatch(r"  (display_name|short_description|default_prompt): (.+)", line)
            if not found:
                errors.append(f"{skill.name}: unsupported UI metadata line")
                continue
            if found[1] in ui:
                errors.append(f"{skill.name}: duplicate UI metadata key")
            try:
                ui[found[1]] = json.loads(found[2])
            except ValueError:
                errors.append(f"{skill.name}: invalid UI string")
        if interface_count != 1:
            errors.append(f"{skill.name}: exactly one UI interface mapping is required")
        if not all(isinstance(ui.get(k), str) and ui[k] for k in ("display_name", "short_description", "default_prompt")):
            errors.append(f"{skill.name}: incomplete UI metadata")
        elif not 25 <= len(ui["short_description"]) <= 64 or f"${name}" not in ui["default_prompt"]:
            errors.append(f"{skill.name}: invalid UI description or invocation")
    try:
        evals = json.loads((skill / "evals" / "evals.json").read_text(encoding="utf-8"))
        if not isinstance(evals, dict) or not isinstance(evals.get("evals"), list):
            raise ValueError("evals must be an object with a list")
        cases = evals["evals"]
        if not all(isinstance(c, dict) and type(c.get("id")) is int for c in cases):
            raise ValueError("cases require integer IDs")
        ids = [c["id"] for c in cases]
        if evals["skill_name"] != name or len(cases) < 2 or len(ids) != len(set(ids)):
            errors.append(f"{skill.name}: invalid eval IDs/name/count")
        for case in cases:
            if not all(isinstance(case.get(key), str) and case[key].strip() for key in ("prompt", "expected_output")):
                errors.append(f"{skill.name}: incomplete eval")
            expectations = case.get("expectations")
            if not isinstance(expectations, list) or not expectations or not all(isinstance(x, str) and x.strip() for x in expectations):
                errors.append(f"{skill.name}: expectations must be a nonempty list of strings")
            files = case.get("files", [])
            if not isinstance(files, list) or not all(isinstance(x, str) for x in files):
                errors.append(f"{skill.name}: files must be a list of paths")
    except (OSError, ValueError, KeyError, TypeError):
        errors.append(f"{skill.name}: missing/malformed evals.json")
    return errors


def validate_project(project_root=ROOT):
    directory = project_root / ".agents" / "skills"
    skills = sorted(path for path in directory.iterdir() if path.is_dir()) if directory.is_dir() else []
    errors = [error for path in skills for error in validate(path, project_root)]
    if not skills:
        errors.append("No project skills found")
    return skills, errors


def main():
    skills, errors = validate_project()
    for error in errors:
        print(error, file=sys.stderr)
    if not errors:
        print(f"OK: {len(skills)} skills; metadata, resource links, UI and eval cases valid.")
    return bool(errors)


if __name__ == "__main__":
    sys.exit(main())
