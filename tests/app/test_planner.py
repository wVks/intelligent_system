"""Independent arithmetic checks for plan outcomes, not response phrasing."""

from datetime import date

import pytest

from studynavigator.planner import build_plan, prepare_task_overrides, tasks_from_card


def tasks(*durations):
    return [{"title": f"Этап {i+1}", "minutes": duration, "estimate_source": "user"}
            for i, duration in enumerate(durations)]


def test_five_hours_cannot_fit_into_two():
    result = build_plan(days=1, minutes_per_day=120, tasks=tasks(300))
    assert result["status"] == "insufficient_time"
    assert result["required_minutes"] == 300
    assert result["available_minutes"] == 120
    assert result["deficit_minutes"] == 180


def test_exact_budget_is_feasible_and_each_day_respects_capacity():
    result = build_plan(days=2, minutes_per_day=60, tasks=tasks(90, 30))
    assert result["status"] == "feasible"
    assert result["deficit_minutes"] == 0
    daily_totals = [sum(task["minutes"] for task in day["tasks"]) for day in result["schedule"]]
    assert sum(daily_totals) == 120
    assert all(0 < total <= 60 for total in daily_totals)


def test_relative_plan_does_not_invent_a_deadline_or_calendar_date():
    result = build_plan(days=3, minutes_per_day=60, tasks=tasks(120))
    assert result["status"] == "feasible"
    assert all(day.get("date") is None for day in result["schedule"])


def test_explicit_start_date_is_respected():
    result = build_plan(days=2, minutes_per_day=60, tasks=tasks(120), start_date=date(2026, 11, 1))
    assert [str(day["date"]) for day in result["schedule"]] == ["2026-11-01", "2026-11-02"]


@pytest.mark.parametrize("days,minutes", [(0, 60), (-1, 60), (1, 0), (1, -60), (True, 60), (1, True)])
def test_invalid_time_budget_is_rejected(days, minutes):
    with pytest.raises(ValueError):
        build_plan(days=days, minutes_per_day=minutes, tasks=tasks(30))


@pytest.mark.parametrize("duration", [0, -10, True])
def test_invalid_task_duration_is_rejected(duration):
    with pytest.raises(ValueError):
        build_plan(days=1, minutes_per_day=60, tasks=tasks(duration))


def test_completed_work_does_not_consume_new_time_or_repeat_in_schedule():
    original = [
        {"id": "research", "title": "Изучить материалы", "minutes": 300, "completed": True},
        {"id": "report", "title": "Написать отчёт", "minutes": 120},
    ]
    result = build_plan(days=2, minutes_per_day=60, tasks=original, assignment_id="astronomy")
    assert result["status"] == "feasible"
    assert result["required_minutes"] == 120
    assert result["completed_minutes"] == 300
    assert result["completed_task_count"] == 1
    assert [task["id"] for day in result["schedule"] for task in day["tasks"]] == ["report", "report"]
    assert not result["remaining_tasks"]
    assert original[1].get("completed") is None  # The calculation does not persist progress.


def test_everything_completed_is_a_valid_empty_schedule():
    complete = [{**task, "completed": True} for task in tasks(90, 30)]
    result = build_plan(days=1, minutes_per_day=15, tasks=complete)
    assert result["status"] == "feasible"
    assert result["required_minutes"] == result["deficit_minutes"] == 0
    assert result["schedule"] == result["remaining_tasks"] == []
    assert result["completed_task_count"] == 2


def test_order_is_preserved_when_tasks_cross_day_boundaries():
    ordered = [{**task, "id": name} for name, task in zip(("first", "second"), tasks(90, 30))]
    result = build_plan(days=2, minutes_per_day=60, tasks=ordered)
    slots = [[(task["id"], task["minutes"]) for task in day["tasks"]] for day in result["schedule"]]
    assert slots == [[("first", 60)], [("first", 30), ("second", 30)]]


def test_another_assignment_never_inherits_the_mcp_project_template():
    with pytest.raises(ValueError, match="confirmed card"):
        build_plan(days=7, minutes_per_day=60, assignment_id="literature-review")
    assert build_plan(days=7, minutes_per_day=120)["required_minutes"] == 570


def test_template_uses_confirmed_assignment_text_with_explicit_estimates():
    card = {
        "goal": {"text": "Исследовать орбиты планет"},
        "deliverable": {"text": "Отчёт и графики орбит"},
        "requirements": [{"text": "Рассчитать орбиту Марса"}, {"text": "Сравнить с наблюдениями"}],
        "tasks": [],
    }
    proposed = tasks_from_card(card)
    assert [task["title"] for task in proposed] == [
        "Выполнить: Рассчитать орбиту Марса", "Выполнить: Сравнить с наблюдениями",
        "Проверить результат: Отчёт и графики орбит",
    ]
    assert [task["minutes"] for task in proposed] == [60, 60, 30]
    assert all(task["estimate_source"] == "estimate" and not task["source_ids"] for task in proposed)
    assert len({task["id"] for task in proposed}) == 3
    assert all("MCP" not in task["title"] for task in proposed)


def test_card_without_requirement_list_still_produces_a_small_content_based_template():
    proposed = tasks_from_card({"goal": {"text": "Сравнить два перевода"},
                                "deliverable": {"text": "Сравнительное эссе"}})
    assert len(proposed) == 2
    assert "Сравнить два перевода" in proposed[0]["title"]
    assert "Сравнительное эссе" in proposed[1]["title"]


def test_existing_card_tasks_are_reused_without_persisted_completion():
    original = {"id": "calculate", "title": "Рассчитать орбиту", "minutes": 75,
                "estimate_source": "estimate", "completed": True}
    proposed = tasks_from_card({"tasks": [original]})
    assert proposed[0]["id"] == "calculate" and proposed[0]["minutes"] == 75
    assert proposed[0]["completed"] is False
    assert original["completed"] is True


def test_browser_cannot_promote_estimates_to_document_facts():
    baseline = [{"id": "write", "title": "Написать эссе", "minutes": 120,
                 "estimate_source": "estimate", "source_ids": []}]
    forged = [{**baseline[0], "estimate_source": "document", "source_ids": ["invented-source"]}]
    prepared = prepare_task_overrides(baseline, forged)
    assert prepared[0]["estimate_source"] == "estimate"
    assert prepared[0]["source_ids"] == []


def test_user_edit_clears_document_estimate_provenance_and_recalculates():
    baseline = [{"id": "practice", "title": "Выполнить упражнение", "minutes": 300,
                 "estimate_source": "document", "source_ids": ["assignment-page-2"]}]
    edited = [{**baseline[0], "minutes": 120, "completed": False}]
    before = build_plan(days=1, minutes_per_day=120, tasks=baseline)
    prepared = prepare_task_overrides(baseline, edited)
    after = build_plan(days=1, minutes_per_day=120, tasks=prepared)
    assert before["deficit_minutes"] == 180
    assert after["status"] == "feasible" and after["deficit_minutes"] == 0
    assert prepared[0]["estimate_source"] == "user" and prepared[0]["source_ids"] == []
    assert baseline[0]["minutes"] == 300


def test_marking_document_task_completed_keeps_unchanged_estimate_source():
    baseline = [{"id": "practice", "title": "Выполнить упражнение", "minutes": 30,
                 "estimate_source": "document", "source_ids": ["assignment-page-2"]}]
    prepared = prepare_task_overrides(baseline, [{**baseline[0], "completed": True}])
    assert prepared[0]["estimate_source"] == "document"
    assert prepared[0]["source_ids"] == ["assignment-page-2"]
    assert build_plan(days=1, minutes_per_day=15, tasks=prepared)["schedule"] == []


def test_browser_reordering_does_not_change_confirmed_execution_order():
    baseline = [{**task, "id": name} for name, task in zip(("research", "write"), tasks(60, 90))]
    prepared = prepare_task_overrides(baseline, list(reversed(baseline)))
    assert [task["id"] for task in prepared] == ["research", "write"]


@pytest.mark.parametrize("changes", [
    [],
    [{"id": "other", "title": "Другой этап", "minutes": 30}],
    [{"id": "one", "title": "Этап", "minutes": 30}, {"id": "one", "title": "Дубликат", "minutes": 30}],
])
def test_missing_unknown_or_duplicate_task_ids_are_rejected(changes):
    with pytest.raises(ValueError):
        prepare_task_overrides([{"id": "one", "title": "Этап", "minutes": 30}], changes)


@pytest.mark.parametrize("completed", ["false", 0, 1, None])
def test_completed_flag_is_not_coerced_from_strings_or_numbers(completed):
    with pytest.raises(ValueError, match="boolean"):
        build_plan(days=1, minutes_per_day=60,
                   tasks=[{"title": "Этап", "minutes": 30, "completed": completed}])


def test_calendar_overflow_is_a_validation_error():
    with pytest.raises(ValueError, match="calendar range"):
        build_plan(days=2, minutes_per_day=60, tasks=tasks(30), start_date=date(9999, 12, 31))
