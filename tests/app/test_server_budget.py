"""Hosted budget wiring through HTTP routes and real local MCP, never paid APIs."""

import asyncio
import json
import time
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from studynavigator.config import Settings
from studynavigator.agent import AgentError, StudyAgent
from studynavigator.live_budget import BudgetTransport
from studynavigator.main import create_app
from studynavigator.providers import ChatCompletionsProvider
from test_ingestion import text_pdf
from test_agent import FixtureGateway, final_turn


def settings(tmp_path, **overrides):
    values = {"database_path": tmp_path / "study.db", "mode": "live",
              "api_base_url": "https://ai.api.cloud.yandex.net/v1", "api_key": "test-only-secret",
              "model": "gpt://test-folder/aliceai-llm-flash", "rate_limit_per_minute": 1000}
    values.update(overrides)
    return Settings(**values)


def response(request, *, usage=True):
    payload = json.loads(request.content)
    if payload.get("response_format"):
        content = {"title": "ORION assignment", "goal": {"text": "Build ORION", "origin": "document", "source_ids": ["S1"]},
                   "deliverable": {"text": "Submit a report", "origin": "document", "source_ids": ["S1"]},
                   "requirements": [], "criteria": [], "deadline": {"date": None, "origin": "unknown", "source_ids": []},
                   "tasks": [], "questions": []}
    else:
        content = {"answer": "Срок неизвестен.", "status": "insufficient_data", "source_ids": [], "facts": []}
    result = {"choices": [{"message": {"role": "assistant", "content": json.dumps(content)}, "finish_reason": "stop"}]}
    if usage:
        result["usage"] = {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}
    return httpx.Response(200, json=result)


def test_package_guard_keeps_the_tools_import_compatible():
    from tools.live_budget import BudgetTransport as ToolBudgetTransport
    assert ToolBudgetTransport is BudgetTransport


def test_budget_config_is_optional_and_reads_only_explicit_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("STUDYNAV_LIVE_BUDGET_RUB", raising=False)
    monkeypatch.delenv("STUDYNAV_LIVE_BUDGET_PATH", raising=False)
    assert Settings.from_env().live_budget_rub is None
    monkeypatch.setenv("STUDYNAV_LIVE_BUDGET_RUB", "15")
    monkeypatch.setenv("STUDYNAV_LIVE_BUDGET_PATH", str(tmp_path / "hosted-budget.json"))
    configured = Settings.from_env()
    assert configured.live_budget_rub == 15
    assert configured.live_budget_path == tmp_path / "hosted-budget.json"


@pytest.mark.parametrize("budget", [0, -1, 500.01, float("nan"), float("inf"), True])
def test_invalid_server_budget_fails_closed(tmp_path, budget):
    with pytest.raises(ValueError):
        settings(tmp_path, live_budget_rub=budget)


@pytest.mark.parametrize("ceiling", [476.9223, 500])
def test_explicit_server_budget_up_to_500_wires_guard_but_is_not_a_default(tmp_path, ceiling):
    assert settings(tmp_path).live_budget_rub is None
    configured = settings(tmp_path, live_budget_rub=ceiling)
    provider = ChatCompletionsProvider(configured, transport=httpx.MockTransport(response))
    app = create_app(configured, provider=provider)
    assert app.state.live_budget is provider.transport
    assert app.state.live_budget.summary()["budget_rub"] == ceiling
    assert app.state.live_budget.budget == Decimal(str(ceiling))
    assert Decimal(json.loads(configured.live_budget_path.read_text())["budget_rub"]) == Decimal(str(ceiling))
    with TestClient(app) as client:
        assert client.post("/api/chat", json={"message": "Когда сдавать?",
                                             "assignment_id": "personal-project"}).status_code == 200
        assert app.state.live_budget.summary()["actual_cost_rub"] == pytest.approx(0.002)


def test_live_eval_cli_still_rejects_above_25_before_loading_credentials(monkeypatch):
    from tools.run_live_eval import main

    def forbidden_settings(cls):
        pytest.fail("Rejected CLI budget must not load credentials or reach a provider")

    monkeypatch.setattr(Settings, "from_env", classmethod(forbidden_settings))
    monkeypatch.setattr("sys.argv", ["run_live_eval.py", "--execute", "--budget-rub", "500"])
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 2


def test_chat_and_analysis_routes_share_one_budget_and_close_it(tmp_path):
    configured = settings(tmp_path, live_budget_rub=15)
    attempts = []

    def dispatch(request):
        attempts.append(request)
        return response(request)

    inner = httpx.MockTransport(dispatch)
    chat = ChatCompletionsProvider(configured, transport=inner)
    analysis = ChatCompletionsProvider(configured, transport=inner)
    app = create_app(configured, provider=chat, analysis_provider=analysis)
    guard = app.state.live_budget
    assert guard is chat.transport is analysis.transport
    assert not guard.tokenize_payload  # Flash uses its conservative byte bound.
    with TestClient(app) as client:
        uploaded = client.post("/api/assignments", files={"files": ("orion.pdf", text_pdf("Build ORION. Submit a report."), "application/pdf")})
        assert uploaded.status_code == 201
        assignment_id = uploaded.json()["assignment"]["assignment_id"]
        analyzed = client.post(f"/api/assignments/{assignment_id}/analyze", json={})
        assert analyzed.status_code == 200
        answered = client.post("/api/chat", json={"message": "Когда сдавать?", "assignment_id": "personal-project"})
        assert answered.status_code == 200
        assert guard.summary()["attempts"] == 2
        assert guard.summary()["actual_cost_rub"] == pytest.approx(0.004)
    assert guard._closed
    assert len(attempts) == 2


def test_unknown_usage_is_reserved_for_browser_requests(tmp_path):
    configured = settings(tmp_path, live_budget_rub=15)
    provider = ChatCompletionsProvider(configured, transport=httpx.MockTransport(lambda request: response(request, usage=False)))
    app = create_app(configured, provider=provider)
    with TestClient(app) as client:
        answered = client.post("/api/chat", json={"message": "Когда сдавать?", "assignment_id": "personal-project"})
        assert answered.status_code == 200
        summary = app.state.live_budget.summary()
        assert summary["attempts"] == 1
        assert summary["actual_cost_rub"] == 0
        assert summary["unknown_reserved_rub"] > 0
        assert summary["remaining_rub"] < 15


def test_split_models_keep_one_existing_ledger_and_charge_each_profile(tmp_path):
    configured = settings(tmp_path, live_budget_rub=15,
                          model="gpt://test-folder/yandexgpt-5.1/latest",
                          analysis_model="gpt://test-folder/aliceai-llm-flash")

    async def previous_request():
        previous = BudgetTransport(configured.live_budget_path, budget_rub=15,
                                   inner=httpx.MockTransport(response))
        async with httpx.AsyncClient(transport=previous) as client:
            await client.post("https://ai.api.cloud.yandex.net/v1/chat/completions", json={
                "model": configured.analysis_model,
                "messages": [{"role": "user", "content": "Earlier request"}], "max_tokens": 10})
        await previous.close()

    asyncio.run(previous_request())
    old_attempt = json.loads(configured.live_budget_path.read_text())["attempts"][0]
    paid_models, tokenizer_models = [], []

    def dispatch(request):
        payload = json.loads(request.content)
        if request.url.path.endswith("/tokenizeCompletion"):
            tokenizer_models.append(payload["modelUri"])
            if payload["modelUri"] == configured.analysis_model:
                return httpx.Response(500, json={"error": "Synthetic unsupported tokenizer"})
            return httpx.Response(200, json={"tokens": [{"id": "1", "text": "x"}] * 80,
                                             "modelVersion": "yagpt-5.1-2025-08"})
        paid_models.append(payload["model"])
        return response(request)

    chat = ChatCompletionsProvider(configured, transport=httpx.MockTransport(dispatch))
    app = create_app(configured, provider=chat)
    guard = app.state.live_budget
    assert guard is chat.transport is app.state.analysis_provider.transport
    assert guard.tokenize_payload
    assert guard.summary()["attempts"] == 1
    with TestClient(app) as client:
        uploaded = client.post("/api/assignments", files={"files": (
            "orion.pdf", text_pdf("Build ORION. Submit a report."), "application/pdf")})
        assert uploaded.status_code == 201
        assignment_id = uploaded.json()["assignment"]["assignment_id"]
        assert client.post(f"/api/assignments/{assignment_id}/analyze", json={}).status_code == 200
        assert client.post("/api/chat", json={"message": "Когда сдавать?",
                                             "assignment_id": "personal-project"}).status_code == 200
        summary = guard.summary()
        assert summary["attempts"] == 3
        # Two Flash calls: 12*.0001 + 4*.0002; one Pro: 16*.0008.
        assert summary["actual_cost_rub"] == pytest.approx(0.0168)
        assert summary["remaining_rub"] == pytest.approx(14.9832)
        assert summary["unknown_reserved_rub"] == 0
        assert not summary["blocked"]
    assert paid_models == tokenizer_models == [configured.analysis_model, configured.model]
    ledger = json.loads(configured.live_budget_path.read_text())
    assert ledger["attempts"][0] == old_attempt
    assert ledger["budget_rub"] == "15"
    flash, pro = ledger["attempts"][1:]
    assert flash["model"] == "aliceai-llm-flash"
    assert flash["estimate_strategy"] == "utf8_bytes"
    assert flash["tokenizer_status"] == "unavailable_or_invalid"
    assert pro["model"] == "yandexgpt-5.1/latest"
    assert pro["estimate_strategy"] == "literal_payload_tokenizer_plus_1024"
    reloaded = BudgetTransport(configured.live_budget_path, budget_rub=15)
    assert reloaded.summary() == summary
    asyncio.run(reloaded.close())


def repair_gateway():
    gateway = FixtureGateway()
    gateway.assignment.update({"is_uploaded": True, "facts": [], "card": {
        "goal": {"text": "Explain the fixture", "origin": "document",
                 "source_ids": ["personal-project-p1"]}}})
    return gateway


@pytest.mark.asyncio
@pytest.mark.parametrize("reject_repair", [False, True])
async def test_format_repair_uses_same_ledger_and_cannot_dispatch_without_reserve(tmp_path, reject_repair):
    configured = settings(tmp_path, model="gpt://test-folder/yandexgpt-5.1/latest")
    paid_payloads = []

    def dispatch(request):
        payload = json.loads(request.content)
        if request.url.path.endswith("/tokenizeCompletion"):
            return httpx.Response(200, json={"tokens": [{"id": "1", "text": "x"}] * 100,
                                             "modelVersion": "yagpt-5.1-2025-08"})
        paid_payloads.append(payload)
        index = len(paid_payloads)
        turn = ({"role": "assistant", "content": "invalid JSON {"} if index == 1
                else final_turn(facts=[], source_ids=["personal-project-p1"]))
        prompt = 950 if reject_repair else 100 * index
        completion = 10 * index
        return httpx.Response(200, json={"choices": [{"message": turn, "finish_reason": "stop"}],
                                        "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                                                  "total_tokens": prompt + completion}})

    guard = BudgetTransport(tmp_path / "repair-budget.json", budget_rub=1.8 if reject_repair else 15,
                            inner=httpx.MockTransport(dispatch), tokenize_payload=True)
    provider = ChatCompletionsProvider(configured, transport=guard)
    try:
        agent = StudyAgent(repair_gateway(), provider)
        if reject_repair:
            with pytest.raises(AgentError) as caught:
                await agent.run("Объясни требования")
            assert caught.value.code == "eval_budget_exceeded"
            assert len(paid_payloads) == guard.summary()["attempts"] == 1
            assert guard.summary()["blocked_reason"] == "insufficient_reservation"
            assert guard.summary()["actual_cost_rub"] == pytest.approx(0.768)
        else:
            result = await agent.run("Объясни требования")
            assert result["format_repair_attempted"] is True
            assert result["usage"] == {"prompt_tokens": 300, "completion_tokens": 30, "total_tokens": 330}
            assert len(paid_payloads) == guard.summary()["attempts"] == 2
            assert guard.summary()["actual_cost_rub"] == pytest.approx(0.264)
            assert guard.summary()["unknown_reserved_rub"] == 0
            assert "tools" not in paid_payloads[1] and "tool_choice" not in paid_payloads[1]
            assert paid_payloads[1]["max_tokens"] == 250
            assert paid_payloads[1]["response_format"] == {"type": "json_object"}
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_initial_and_repair_share_one_outer_deadline_and_keep_unknown_reserve(tmp_path):
    configured = settings(tmp_path, model="gpt://test-folder/yandexgpt-5.1/latest", provider_timeout=1)
    paid_payloads = []

    async def dispatch(request):
        payload = json.loads(request.content)
        if request.url.path.endswith("/tokenizeCompletion"):
            return httpx.Response(200, json={"tokens": [{"id": "1", "text": "x"}] * 100,
                                             "modelVersion": "yagpt-5.1-2025-08"})
        paid_payloads.append(payload)
        index = len(paid_payloads)
        # Each call fits 100 ms separately; the pair does not fit one deadline.
        await asyncio.sleep(0.04 if index == 1 else 0.08)
        turn = ({"role": "assistant", "content": "invalid JSON {"} if index == 1
                else final_turn(facts=[], source_ids=["personal-project-p1"]))
        return httpx.Response(200, json={"choices": [{"message": turn, "finish_reason": "stop"}],
                                        "usage": {"prompt_tokens": 100, "completion_tokens": 10,
                                                  "total_tokens": 110}})

    guard = BudgetTransport(tmp_path / "deadline-budget.json", budget_rub=15,
                            inner=httpx.MockTransport(dispatch), tokenize_payload=True)
    provider = ChatCompletionsProvider(configured, transport=guard)
    started = time.monotonic()
    try:
        with pytest.raises(AgentError) as caught:
            await StudyAgent(repair_gateway(), provider, request_timeout=0.1).run("Объясни требования")
        assert caught.value.code == "request_timeout"
        assert time.monotonic() - started < 0.5
        assert len(paid_payloads) == guard.summary()["attempts"] == 2
        assert guard.summary()["actual_cost_rub"] == pytest.approx(0.088)
        assert guard.summary()["unknown_reserved_rub"] > 0
        assert not guard.summary()["blocked"]
        assert json.loads(guard.ledger_path.read_text())["attempts"][1]["status"] == "unknown"
    finally:
        await guard.close()


def test_exhausted_budget_returns_controlled_503_for_both_http_routes(tmp_path):
    configured = settings(tmp_path, live_budget_rub=0.001)
    dispatched = []
    provider = ChatCompletionsProvider(configured, transport=httpx.MockTransport(lambda request: dispatched.append(request) or response(request)))
    app = create_app(configured, provider=provider)
    with TestClient(app) as client:
        answered = client.post("/api/chat", json={"message": "Когда сдавать?", "assignment_id": "personal-project"})
        assert answered.status_code == 503
        assert answered.json()["error"]["code"] == "eval_budget_exceeded"
        uploaded = client.post("/api/assignments", files={"files": ("orion.pdf", text_pdf("Build ORION. Submit a report."), "application/pdf")})
        assert uploaded.status_code == 201
        assignment_id = uploaded.json()["assignment"]["assignment_id"]
        analyzed = client.post(f"/api/assignments/{assignment_id}/analyze", json={})
        assert analyzed.status_code == 503
        assert analyzed.json()["error"]["code"] == "eval_budget_exceeded"
        assert client.get("/health").status_code == 200
        assert not dispatched
        assert app.state.live_budget.summary()["attempts"] == 0


def test_manual_confirmation_plan_and_material_search_work_after_budget_error_without_llm(tmp_path):
    configured = settings(tmp_path, live_budget_rub=0.001)
    dispatched = []
    provider = ChatCompletionsProvider(configured, transport=httpx.MockTransport(
        lambda request: dispatched.append(request) or response(request)))
    app = create_app(configured, provider=provider)
    with TestClient(app) as client:
        uploaded = client.post("/api/assignments", files={"files": (
            "orion.pdf", text_pdf("Build ORION. Submit a report."), "application/pdf")})
        assignment = uploaded.json()["assignment"]
        assignment_id = assignment["assignment_id"]
        assert assignment["card"] is None and assignment["card_revision"] == 0
        failed = client.post(f"/api/assignments/{assignment_id}/analyze", json={})
        assert failed.status_code == 503 and failed.json()["error"]["code"] == "eval_budget_exceeded"
        assert app.state.live_budget.summary()["blocked"]
        # A local blank form can be confirmed directly; no successful /analyze is required.
        card = {"title": assignment["title"], "goal": {"text": "Build ORION", "origin": "user", "source_ids": []},
                "deliverable": {"text": "Submit a report", "origin": "user", "source_ids": []},
                "requirements": [], "criteria": [], "deadline": {"date": None, "origin": "unknown", "source_ids": []},
                "tasks": [], "questions": []}
        confirmed = client.post(f"/api/assignments/{assignment_id}/confirm", json={"card": card, "revision": 0})
        assert confirmed.status_code == 200
        ready = confirmed.json()["assignment"]
        assert ready["status"] == "ready" and ready["card_revision"] == 1
        assert ready["card"]["goal"]["origin"] == "user" and ready["card"]["goal"]["source_ids"] == []
        assert ready["card"]["deadline"] == {"date": None, "origin": "unknown", "source_ids": []}
        assert ready["card"]["tasks"] and all(task["estimate_source"] == "estimate" for task in ready["card"]["tasks"])
        planned = client.post("/api/plan", json={"assignment_id": assignment_id, "card_revision": 1,
                                                 "days": 1, "minutes_per_day": 120})
        assert planned.status_code == 200
        materials = client.get(f"/api/assignments/{assignment_id}/materials", params={"q": "ORION"})
        assert materials.status_code == 200 and materials.json()["matches"]
        assert not dispatched and app.state.live_budget.summary()["attempts"] == 0


def test_default_disabled_guard_preserves_existing_provider_transport(tmp_path):
    configured = settings(tmp_path)
    inner = httpx.MockTransport(response)
    provider = ChatCompletionsProvider(configured, transport=inner)
    app = create_app(configured, provider=provider)
    assert app.state.live_budget is None
    assert provider.transport is inner
    with TestClient(app) as client:
        assert client.post("/api/chat", json={"message": "Когда сдавать?", "assignment_id": "personal-project"}).status_code == 200
    assert not configured.live_budget_path.exists()


def test_offline_does_not_create_a_paid_budget_ledger(tmp_path):
    configured = settings(tmp_path, mode="offline", live_budget_rub=15)
    app = create_app(configured)
    assert app.state.live_budget is None
    assert not configured.live_budget_path.exists()


def test_startup_failure_still_closes_the_guard(monkeypatch, tmp_path):
    configured = settings(tmp_path, live_budget_rub=15)

    class UnavailableMCP:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            raise RuntimeError("Synthetic MCP startup failure")

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr("studynavigator.main.MCPGateway", UnavailableMCP)
    app = create_app(configured)
    with pytest.raises(RuntimeError, match="Synthetic MCP"):
        with TestClient(app):
            pass
    assert app.state.live_budget._closed
