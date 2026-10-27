"""Hosted budget wiring through HTTP routes and real local MCP, never paid APIs."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from studynavigator.config import Settings
from studynavigator.live_budget import BudgetTransport
from studynavigator.main import create_app
from studynavigator.providers import ChatCompletionsProvider
from test_ingestion import text_pdf


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


@pytest.mark.parametrize("budget", [0, -1, 26, float("nan"), float("inf"), True])
def test_invalid_server_budget_fails_closed(tmp_path, budget):
    with pytest.raises(ValueError):
        settings(tmp_path, live_budget_rub=budget)


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
