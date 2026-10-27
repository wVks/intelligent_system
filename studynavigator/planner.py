"""Deterministic relative planning in whole minutes. Estimates are not course facts."""

import re
from datetime import date, timedelta

DEFAULT_TASKS = [
    {"title": "Уточнить задачу и подготовить материалы", "minutes": 60, "estimate_source": "estimate"},
    {"title": "Реализовать MCP и AI-логику", "minutes": 180, "estimate_source": "estimate"},
    {"title": "Собрать веб-интерфейс", "minutes": 90, "estimate_source": "estimate"},
    {"title": "Провести Eval и разобрать ошибки", "minutes": 90, "estimate_source": "estimate"},
    {"title": "Развернуть сервис и проверить запуск", "minutes": 60, "estimate_source": "estimate"},
    {"title": "Оформить README и подготовить защиту", "minutes": 90, "estimate_source": "estimate"},
]


def _normalize_tasks(tasks):
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 20:
        raise ValueError("provide 1 to 20 tasks")
    normalized = []
    identifiers = set()
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise ValueError("task must be an object")
        minutes, title = task.get("minutes"), task.get("title")
        origin = task.get("estimate_source", "user")
        completed = task.get("completed", False)
        identifier = task.get("id") if task.get("id") is not None else f"task-{index + 1}"
        source_ids = task.get("source_ids", [])
        if type(minutes) is not int or not 1 <= minutes <= 4800:
            raise ValueError("task duration must be a positive integer, at most 4800 minutes")
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise ValueError("task title must contain 1 to 200 characters")
        if origin not in {"estimate", "user", "document"}:
            raise ValueError("invalid estimate source")
        if type(completed) is not bool:
            raise ValueError("completed must be boolean")
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identifier):
            raise ValueError("invalid task id")
        if identifier in identifiers:
            raise ValueError("task ids must be unique")
        if (not isinstance(source_ids, list) or len(source_ids) > 5
                or any(not isinstance(ref, str) or not ref or len(ref) > 100 for ref in source_ids)):
            raise ValueError("invalid estimate sources")
        identifiers.add(identifier)
        normalized.append({"id": identifier, "title": title.strip(), "minutes": minutes,
                           "estimate_source": origin, "source_ids": list(dict.fromkeys(source_ids)),
                           "completed": completed})
    return normalized


def _field_text(value):
    if isinstance(value, dict):
        text = value.get("text")
        return text.strip() if isinstance(text, str) else ""
    return value.strip() if isinstance(value, str) else ""


def tasks_from_card(card):
    """Use proposed tasks or a visible, editable template from this card's text.

    The fallback does not interpret documents or pretend that its durations came
    from a model. The confirmed requirement order is the execution order.
    """
    if not isinstance(card, dict):
        raise ValueError("card must be an object")
    if card.get("tasks"):
        if not isinstance(card["tasks"], list) or any(not isinstance(task, dict) for task in card["tasks"]):
            raise ValueError("proposed tasks must be objects")
        # Completion belongs to the current calculation, not the persisted card.
        return _normalize_tasks([{**task, "completed": False} for task in card["tasks"]])
    requirements = card.get("requirements") or []
    if not isinstance(requirements, list):
        raise ValueError("requirements must be a list")
    texts = [_field_text(item) for item in requirements]
    texts = [text for text in texts if text]
    if len(texts) > 19:
        raise ValueError("too many requirements to plan without omitting work")
    result = [{"id": f"task-{index + 1}", "title": ("Выполнить: " + text)[:200],
               "minutes": 60, "estimate_source": "estimate"}
              for index, text in enumerate(texts)]
    if not result:
        goal = _field_text(card.get("goal"))
        if not goal:
            raise ValueError("confirmed card needs a goal or requirements to propose tasks")
        result.append({"id": "task-1", "title": ("Подготовить: " + goal)[:200],
                       "minutes": 60, "estimate_source": "estimate"})
    deliverable = _field_text(card.get("deliverable"))
    if not deliverable:
        raise ValueError("confirmed card needs a deliverable to propose tasks")
    result.append({"id": f"task-{len(result) + 1}",
                   "title": ("Проверить результат: " + deliverable)[:200],
                   "minutes": 30, "estimate_source": "estimate"})
    return _normalize_tasks(result)


def prepare_task_overrides(base_tasks, overrides=None):
    """Derive provenance from trusted tasks, never from browser claims.

    All tasks stay present and in their confirmed order. Completed work is
    explicitly excluded by the scheduler; it is not silently dropped by input.
    """
    base = _normalize_tasks(base_tasks)
    if overrides is None:
        return base
    submitted = _normalize_tasks(overrides)
    if len(submitted) != len(base):
        raise ValueError("provide all tasks; use completed for finished work")
    by_id = {task["id"]: task for task in submitted}
    if set(by_id) != {task["id"] for task in base}:
        raise ValueError("task ids must match the confirmed card")
    result = []
    for original in base:
        update = by_id[original["id"]]
        unchanged = (update["title"] == original["title"] and update["minutes"] == original["minutes"])
        result.append({**original, "title": update["title"], "minutes": update["minutes"],
                       "completed": update["completed"],
                       "estimate_source": original["estimate_source"] if unchanged else "user",
                       "source_ids": original["source_ids"] if unchanged else []})
    return result


def build_plan(*, days, minutes_per_day, tasks=None, start_date=None, assignment_id="personal-project"):
    if type(days) is not int or not 1 <= days <= 30:
        raise ValueError("days must be an integer between 1 and 30")
    if type(minutes_per_day) is not int or not 15 <= minutes_per_day <= 480:
        raise ValueError("minutes_per_day must be an integer between 15 and 480")
    if isinstance(start_date, str):
        start_date = date.fromisoformat(start_date)
    if start_date is not None and type(start_date) is not date:
        raise ValueError("start_date must be an ISO date")
    if start_date is not None:
        try:
            start_date + timedelta(days=days - 1)
        except OverflowError as exc:
            raise ValueError("start_date is outside the supported calendar range") from exc
    if tasks is None and assignment_id != "personal-project":
        raise ValueError("tasks for this assignment must come from its confirmed card")
    normalized = _normalize_tasks(DEFAULT_TASKS if tasks is None else tasks)
    pending = [task for task in normalized if not task["completed"]]
    required = sum(t["minutes"] for t in pending)
    available = days * minutes_per_day
    remaining = [t["minutes"] for t in pending]
    schedule = []
    cursor = 0
    for day in range(1, days + 1) if pending else []:
        budget, slots = minutes_per_day, []
        while cursor < len(pending) and budget:
            take = min(remaining[cursor], budget)
            slots.append({**pending[cursor], "minutes": take})
            remaining[cursor] -= take
            budget -= take
            if remaining[cursor] == 0:
                cursor += 1
        schedule.append({"day": day, "date": (start_date + timedelta(days=day - 1)).isoformat()
                         if start_date else None, "tasks": slots})
    return {
        "assignment_id": assignment_id,
        "status": "feasible" if required <= available else "insufficient_time",
        "available_minutes": available, "required_minutes": required,
        "deficit_minutes": max(0, required - available), "schedule": schedule,
        "tasks": normalized,
        "completed_task_count": len(normalized) - len(pending),
        "completed_minutes": sum(task["minutes"] for task in normalized if task["completed"]),
        "remaining_tasks": [{**task, "minutes": rest} for task, rest in zip(pending, remaining) if rest],
        "assumptions": ["Длительности с меткой estimate — предварительные оценки, а не требования преподавателя; user — ваши оценки.",
                        "План относительный; даты дней появляются только при явно заданной дате начала.",
                        "Этапы выполняются последовательно и могут делиться между днями."],
    }
