"""Card → confirmation → plan through HTTP and real MCP, with no paid model.

The injected analysis provider is a deterministic protocol fixture. Its mode is
named live only to exercise the production branch; these are not live AI Evals.
"""

import copy

import pytest
from fastapi.testclient import TestClient

from studynavigator.config import Settings
from studynavigator.main import create_app
from studynavigator.providers import ProviderError
from test_ingestion import text_pdf


class AnalysisFixture:
    mode = "live"
    model = "fixture-only"

    def __init__(self):
        self.calls = []
        self.mutate = None
        self.failure = None

    async def analyze(self, context):
        assert set(context) == {"assignment_id", "title", "sources"}
        assert context["sources"]
        self.calls.append(copy.deepcopy(context))
        if self.failure is not None:
            raise self.failure
        first = context["sources"][0]["source_id"]
        second = context["sources"][-1]["source_id"]
        card = {
            "title": "ORION orbit simulation",
            "goal": {"text": "Build an ORION orbit simulation", "origin": "document", "source_ids": [first]},
            "deliverable": {"text": "Submit a notebook", "origin": "document", "source_ids": [first]},
            "requirements": [{"text": "Build the simulation", "origin": "document", "source_ids": [first]}],
            "criteria": [{"text": "Simulation accuracy", "origin": "document", "source_ids": [second]}],
            "deadline": {"date": None, "origin": "unknown", "source_ids": []},
            "tasks": [
                {"id": "prepare", "title": "Prepare simulation", "minutes": 90,
                 "estimate_source": "document", "source_ids": [second]},
                {"id": "notebook", "title": "Write the notebook", "minutes": 210,
                 "estimate_source": "estimate", "source_ids": []},
            ],
            "questions": ["When is the assignment due?"],
        }
        if self.mutate:
            self.mutate(card)
        return {"card": card, "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}


class NoModel:
    mode = "offline"
    model = None

    async def complete(self, *args, **kwargs):
        raise AssertionError("These API tests must not invoke a chat model")

    async def analyze(self, *args, **kwargs):
        raise AssertionError("Offline must not pretend to analyze an uploaded document")


@pytest.fixture
def settings(tmp_path):
    return Settings(database_path=tmp_path / "study.sqlite", rate_limit_per_minute=1000)


@pytest.fixture
def analysis():
    return AnalysisFixture()


@pytest.fixture
def client(settings, analysis):
    with TestClient(create_app(settings=settings, provider=NoModel(), analysis_provider=analysis)) as browser:
        browser.get("/")
        yield browser


def upload(browser, title="Astronomy assignment"):
    response = browser.post("/api/assignments", data={"title": title}, files=[
        ("files", ("orbits.pdf", text_pdf(
            "Build an ORION orbit simulation. Submit a notebook.",
            "Assess simulation accuracy. Preparation takes 90 minutes.",
        ), "application/pdf")),
    ])
    assert response.status_code == 201, response.text
    return response.json()["assignment"]


def analyze(browser, assignment):
    response = browser.post(f"/api/assignments/{assignment['assignment_id']}/analyze", json={})
    assert response.status_code == 200, response.text
    return response.json()


def confirm(browser, assignment, draft, card=None):
    response = browser.post(f"/api/assignments/{assignment['assignment_id']}/confirm", json={
        "card": copy.deepcopy(card if card is not None else draft["card"]),
        "revision": draft["revision"],
    })
    assert response.status_code == 200, response.text
    return response.json()["assignment"]


def plan(browser, assignment, **changes):
    return browser.post("/api/plan", json={
        "assignment_id": assignment["assignment_id"], "card_revision": assignment["card_revision"],
        "days": 1, "minutes_per_day": 120, **changes,
    })


def test_analysis_confirmation_and_plan_use_the_uploaded_assignment(client, analysis):
    assignment = upload(client)
    draft = analyze(client, assignment)
    assert draft["assignment"]["status"] == "needs_confirmation"
    assert draft["revision"] == assignment["card_revision"] + 1
    assert draft["card"]["deadline"]["date"] is None
    assert draft["mode"] == "live"  # Injected fixture branch, not an actual network call.
    assert draft["usage"]["total_tokens"] == 150
    assert draft["trace"][0]["name"] == "get_assignment" and draft["trace"][0]["status"] == "ok"
    assert analysis.calls[0]["assignment_id"] == assignment["assignment_id"]
    assert {s["source_id"] for s in analysis.calls[0]["sources"]} == set(assignment["source_ids"])

    confirmed = confirm(client, assignment, draft)
    assert confirmed["status"] == "ready"
    assert confirmed["card_revision"] == draft["revision"] + 1
    assert confirmed["due_date"] is confirmed["due_at"] is None
    assert confirmed["facts"] == []  # No rubric numbers inherited from the prepared project.
    assert confirmed["card"]["goal"]["origin"] == "document"
    response = plan(client, confirmed)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "insufficient_time"
    assert result["required_minutes"] == 300 and result["deficit_minutes"] == 180
    assert [task["id"] for task in result["tasks"]] == ["prepare", "notebook"]
    assert result["tasks"][0]["estimate_source"] == "document"


def test_offline_card_is_manual_and_unknown_deadline_stays_unknown(settings):
    with TestClient(create_app(settings=settings, provider=NoModel())) as browser:
        assignment = upload(browser)
        draft = analyze(browser, assignment)
        assert draft["mode"] == "offline" and draft["usage"] is None
        assert "не подключена" in draft["notice"].lower()
        assert draft["card"]["goal"]["text"] == ""
        assert draft["card"]["requirements"] == draft["card"]["tasks"] == []
        assert draft["trace"][0]["name"] == "get_assignment"
        incomplete = browser.post(f"/api/assignments/{assignment['assignment_id']}/confirm", json={
            "card": draft["card"], "revision": draft["revision"],
        })
        assert incomplete.status_code == 422
        card = copy.deepcopy(draft["card"])
        card["goal"]["text"] = "Compare two translations"
        card["deliverable"]["text"] = "A comparative essay"
        card["requirements"] = [{"text": "Compare the introduction", "origin": "user", "source_ids": []}]
        ready = confirm(browser, assignment, draft, card)
        assert ready["due_date"] is None
        assert ready["card"]["goal"]["origin"] == "user"
        assert ready["card"]["tasks"]
        assert all(task["estimate_source"] == "estimate" for task in ready["card"]["tasks"])
        assert all("MCP" not in task["title"] for task in ready["card"]["tasks"])
        response = plan(browser, ready)
        assert response.status_code == 200
        assert response.json()["required_minutes"] == 90


def test_user_edits_are_not_promoted_to_document_facts(client):
    assignment = upload(client)
    draft = analyze(client, assignment)
    edited = copy.deepcopy(draft["card"])
    edited["goal"]["text"] = "My revised ORION goal"
    edited["deadline"] = {"date": "2027-01-25", "origin": "document",
                          "source_ids": [assignment["source_ids"][0]]}
    ready = confirm(client, assignment, draft, edited)
    assert ready["card"]["goal"] == {"text": "My revised ORION goal", "origin": "user", "source_ids": []}
    assert ready["card"]["deliverable"] == draft["card"]["deliverable"]
    assert ready["card"]["deadline"] == {"date": "2027-01-25", "origin": "user", "source_ids": []}


def test_plan_completion_and_edits_are_current_calculation_only(client):
    assignment = upload(client)
    ready = confirm(client, assignment, analyze(client, assignment))
    tasks = copy.deepcopy(ready["card"]["tasks"])
    tasks[0]["completed"] = True
    tasks[1]["minutes"] = 120
    response = plan(client, ready, tasks=tasks)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["required_minutes"] == 120 and result["status"] == "feasible"
    assert result["completed_task_count"] == 1
    assert [task["id"] for day in result["schedule"] for task in day["tasks"]] == ["notebook"]
    assert result["tasks"][1]["estimate_source"] == "user"
    persisted = client.get(f"/api/assignments/{ready['assignment_id']}").json()["assignment"]
    assert persisted["card"]["tasks"] == ready["card"]["tasks"]

    for task in tasks:
        task["completed"] = True
    complete = plan(client, ready, tasks=tasks)
    assert complete.status_code == 200
    assert complete.json()["required_minutes"] == complete.json()["deficit_minutes"] == 0
    assert complete.json()["schedule"] == []


def test_browser_cannot_forge_document_time_provenance(client):
    assignment = upload(client)
    ready = confirm(client, assignment, analyze(client, assignment))
    tasks = copy.deepcopy(ready["card"]["tasks"])
    tasks[0]["minutes"] = 60
    tasks[1].update(estimate_source="document", source_ids=[assignment["source_ids"][0]])
    response = plan(client, ready, tasks=tasks)
    assert response.status_code == 200, response.text
    normalized = response.json()["tasks"]
    assert normalized[0]["estimate_source"] == "user" and normalized[0]["source_ids"] == []
    assert normalized[1]["estimate_source"] == "estimate" and normalized[1]["source_ids"] == []


def test_card_revisions_reject_stale_confirmation_and_plan(client):
    assignment = upload(client)
    draft = analyze(client, assignment)
    ready = confirm(client, assignment, draft)
    stale_confirm = client.post(f"/api/assignments/{assignment['assignment_id']}/confirm", json={
        "card": draft["card"], "revision": draft["revision"],
    })
    assert stale_confirm.status_code == 409
    stale_plan = plan(client, ready, card_revision=draft["revision"])
    assert stale_plan.status_code == 409
    absent_revision = client.post("/api/plan", json={"assignment_id": ready["assignment_id"],
                                                  "days": 2, "minutes_per_day": 120})
    assert absent_revision.status_code in {409, 422}
    fresh = client.get(f"/api/assignments/{ready['assignment_id']}").json()["assignment"]
    assert fresh["card_revision"] == ready["card_revision"] and fresh["status"] == "ready"


def test_pending_cards_cannot_be_planned(client):
    assignment = upload(client)
    assert plan(client, assignment).status_code == 409
    draft = analyze(client, assignment)
    assert plan(client, draft["assignment"]).status_code == 409


def test_failed_analysis_does_not_overwrite_last_confirmed_card(client, analysis):
    assignment = upload(client)
    ready = confirm(client, assignment, analyze(client, assignment))
    analysis.failure = ProviderError("provider_unavailable", "The fixture is unavailable")
    response = client.post(f"/api/assignments/{ready['assignment_id']}/analyze", json={})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_unavailable"
    actual = client.get(f"/api/assignments/{ready['assignment_id']}").json()["assignment"]
    assert actual["card_revision"] == ready["card_revision"]
    assert actual["card"] == ready["card"] and actual["status"] == "ready"


def test_unknown_or_foreign_analysis_source_is_rejected_before_saving(client, analysis):
    first = upload(client)
    second = upload(client, title="Another assignment")
    foreign_id = second["source_ids"][0]
    analysis.mutate = lambda card: card["goal"].update(source_ids=[foreign_id])
    response = client.post(f"/api/assignments/{first['assignment_id']}/analyze", json={})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_source"
    after = client.get(f"/api/assignments/{first['assignment_id']}").json()["assignment"]
    assert after["card_revision"] == first["card_revision"] and after["card"] is None


def test_foreign_session_cannot_analyze_confirm_or_plan(settings, analysis):
    app = create_app(settings=settings, provider=NoModel(), analysis_provider=analysis)
    with TestClient(app) as owner, TestClient(app) as stranger:
        assignment = upload(owner)
        draft = analyze(owner, assignment)
        ready = confirm(owner, assignment, draft)
        calls_before = len(analysis.calls)
        for response in [
            stranger.post(f"/api/assignments/{ready['assignment_id']}/analyze", json={}),
            stranger.post(f"/api/assignments/{ready['assignment_id']}/confirm", json={
                "card": ready["card"], "revision": ready["card_revision"],
            }),
            plan(stranger, ready),
        ]:
            assert response.status_code == 404
            assert response.json()["request_id"]
        assert len(analysis.calls) == calls_before


def test_confirmed_card_and_its_provenance_survive_restart(settings, analysis):
    with TestClient(create_app(settings=settings, provider=NoModel(), analysis_provider=analysis)) as first:
        assignment = upload(first)
        ready = confirm(first, assignment, analyze(first, assignment))
        cookies = dict(first.cookies.items())
    with TestClient(create_app(settings=settings, provider=NoModel(), analysis_provider=analysis)) as restarted:
        restarted.cookies.update(cookies)
        response = restarted.get(f"/api/assignments/{ready['assignment_id']}")
        assert response.status_code == 200
        actual = response.json()["assignment"]
        assert actual["card"] == ready["card"] and actual["card_revision"] == ready["card_revision"]
        assert plan(restarted, actual).json()["deficit_minutes"] == 180


def test_seed_custom_plan_keeps_requested_minutes_without_document_claims(settings):
    with TestClient(create_app(settings=settings, provider=NoModel())) as browser:
        response = browser.post("/api/plan", json={"days": 1, "minutes_per_day": 120, "tasks": [{
            "title": "My custom work", "minutes": 300, "estimate_source": "document", "source_ids": [],
        }]})
        assert response.status_code == 200
        result = response.json()
        assert result["required_minutes"] == 300 and result["deficit_minutes"] == 180
        assert result["tasks"][0]["estimate_source"] == "user"
        assert result["tasks"][0]["source_ids"] == []


def test_confirmed_upload_chat_uses_its_card_and_real_mcp_search(settings, analysis):
    with TestClient(create_app(settings=settings, analysis_provider=analysis)) as browser:
        assignment = upload(browser)
        ready = confirm(browser, assignment, analyze(browser, assignment))
        known_ids = set(assignment["source_ids"])
        requirements = browser.post("/api/chat", json={
            "assignment_id": ready["assignment_id"], "message": "Какие требования к заданию ORION?",
        })
        assert requirements.status_code == 200, requirements.text
        result = requirements.json()
        assert result["mode"] == "offline" and result["status"] == "answered"
        assert "Submit a notebook" in result["answer"] and "Build the simulation" in result["answer"]
        assert {source["source_id"] for source in result["sources"]} <= known_ids
        assert result["sources"] and result["trace"][0]["name"] == "get_assignment"
        assert "Личный проект" not in result["answer"] and "50 баллов" not in result["answer"]

        searched = browser.post("/api/chat", json={"assignment_id": ready["assignment_id"], "message": "ORION"})
        assert searched.status_code == 200, searched.text
        found = searched.json()
        assert any(step["name"] == "search_materials" and step["status"] == "ok" for step in found["trace"])
        assert "ORION" in found["answer"]
        assert {source["source_id"] for source in found["sources"]} <= known_ids
        assert all(source["source_id"].startswith("src-") for source in found["sources"])

        absent = browser.post("/api/chat", json={
            "assignment_id": ready["assignment_id"], "message": "Сколько баллов за MCP?",
        })
        assert absent.status_code == 200
        assert absent.json()["status"] == "insufficient_data"
        assert "10 баллов" not in absent.json()["answer"] and "50 баллов" not in absent.json()["answer"]


def test_uploaded_chat_labels_user_deadline_instead_of_claiming_document_evidence(settings, analysis):
    with TestClient(create_app(settings=settings, analysis_provider=analysis)) as browser:
        assignment = upload(browser)
        draft = analyze(browser, assignment)
        edited = copy.deepcopy(draft["card"])
        edited["deadline"] = {"date": "2027-01-25", "origin": "user", "source_ids": []}
        ready = confirm(browser, assignment, draft, edited)
        response = browser.post("/api/chat", json={
            "assignment_id": ready["assignment_id"], "message": "Когда сдавать задание?",
        })
        assert response.status_code == 200, response.text
        result = response.json()
        assert "2027-01-25" in result["answer"] and "Вы указали" in result["answer"]
        assert "не подтверждённый срок документа" in result["answer"]
        assert result["status"] == "insufficient_data" and result["sources"] == result["facts"] == []


def test_uploaded_unknown_deadline_is_not_filled_from_chat_history(settings, analysis):
    with TestClient(create_app(settings=settings, analysis_provider=analysis)) as browser:
        assignment = upload(browser)
        ready = confirm(browser, assignment, analyze(browser, assignment))
        response = browser.post("/api/chat", json={
            "assignment_id": ready["assignment_id"], "message": "Когда сдавать задание?",
            "history": [{"role": "assistant", "content": "Дата сдачи — 2027-01-25."}],
        })
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "insufficient_data"
        assert "2027-01-25" not in result["answer"]
        assert result["sources"] == result["facts"] == []


def test_explicit_start_and_known_deadline_bound_available_planning_days(client):
    assignment = upload(client)
    draft = analyze(client, assignment)
    edited = copy.deepcopy(draft["card"])
    edited["deadline"] = {"date": "2027-04-11", "origin": "user", "source_ids": []}
    ready = confirm(client, assignment, draft, edited)
    response = plan(client, ready, days=7, minutes_per_day=120, start_date="2027-04-10")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["required_minutes"] == 300
    assert result["available_minutes"] == 240 and result["deficit_minutes"] == 60
    assert result["status"] == "insufficient_time"
    assert [day["date"] for day in result["schedule"]] == ["2027-04-10", "2027-04-11"]


def test_explicit_start_after_known_deadline_is_rejected(client):
    assignment = upload(client)
    draft = analyze(client, assignment)
    edited = copy.deepcopy(draft["card"])
    edited["deadline"] = {"date": "2027-04-11", "origin": "user", "source_ids": []}
    ready = confirm(client, assignment, draft, edited)
    response = plan(client, ready, days=7, start_date="2027-04-12")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_plan"


def test_all_completed_work_needs_no_schedule_even_after_known_deadline(client):
    assignment = upload(client)
    draft = analyze(client, assignment)
    edited = copy.deepcopy(draft["card"])
    edited["deadline"] = {"date": "2027-04-11", "origin": "user", "source_ids": []}
    ready = confirm(client, assignment, draft, edited)
    completed = [{**task, "completed": True} for task in ready["card"]["tasks"]]
    response = plan(client, ready, days=7, start_date="2027-04-12", tasks=completed)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "feasible"
    assert result["required_minutes"] == result["deficit_minutes"] == 0
    assert result["schedule"] == result["remaining_tasks"] == []
