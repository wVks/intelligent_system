"""Explicit model roles: no automatic switching, no paid requests."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from studynavigator.config import Settings
from studynavigator import main
from studynavigator.providers import ChatCompletionsProvider, OfflineProvider


CHAT_MODEL = "gpt://chat-folder/yandexgpt-5.1/latest"
ANALYSIS_MODEL = "gpt://analysis-folder/aliceai-llm-flash"


def settings(tmp_path, **overrides):
    return Settings(**({"database_path": tmp_path / "study.db", "mode": "live",
                        "api_base_url": "https://ai.api.cloud.yandex.net/v1",
                        "api_key": "synthetic-role-test-key", "model": CHAT_MODEL} | overrides))


def install_mock_provider(monkeypatch):
    constructed, observed = [], []

    def dispatch(request):
        observed.append(request)
        content = {"title": "Fixture card"} if "response_format" in json.loads(request.content) else {"answer": "Fixture"}
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": json.dumps(content)},
                                                      "finish_reason": "stop"}]})

    class MockProvider(ChatCompletionsProvider):
        def __init__(self, configured):
            constructed.append(configured.model)
            super().__init__(configured, transport=httpx.MockTransport(dispatch))

    monkeypatch.setattr(main, "ChatCompletionsProvider", MockProvider)
    return constructed, observed


def test_optional_analysis_model_reads_only_explicit_environment(monkeypatch):
    monkeypatch.delenv("LLM_ANALYSIS_MODEL", raising=False)
    assert Settings.from_env().analysis_model == ""
    monkeypatch.setenv("LLM_ANALYSIS_MODEL", ANALYSIS_MODEL)
    assert Settings.from_env().analysis_model == ANALYSIS_MODEL


@pytest.mark.parametrize("analysis_model", ["", CHAT_MODEL])
def test_default_or_matching_model_reuses_the_single_provider(monkeypatch, tmp_path, analysis_model):
    constructed, observed = install_mock_provider(monkeypatch)
    app = main.create_app(settings(tmp_path, analysis_model=analysis_model))
    assert app.state.provider is app.state.analysis_provider
    assert constructed == [CHAT_MODEL]
    assert observed == []


def test_offline_ignores_configured_model_roles_without_creating_live_provider(monkeypatch, tmp_path):
    constructed, observed = install_mock_provider(monkeypatch)
    app = main.create_app(settings(tmp_path, mode="offline", analysis_model=ANALYSIS_MODEL))
    assert isinstance(app.state.provider, OfflineProvider)
    assert app.state.analysis_provider is app.state.provider
    assert constructed == observed == []


def test_explicit_analysis_provider_takes_precedence_over_environment_role(monkeypatch, tmp_path):
    constructed, observed = install_mock_provider(monkeypatch)
    injected = object()
    app = main.create_app(settings(tmp_path, analysis_model=ANALYSIS_MODEL), analysis_provider=injected)
    assert app.state.analysis_provider is injected
    assert constructed == [CHAT_MODEL]
    assert observed == []


@pytest.mark.asyncio
async def test_separate_roles_send_only_to_the_configured_model_and_derive_each_folder(monkeypatch, tmp_path):
    constructed, observed = install_mock_provider(monkeypatch)
    app = main.create_app(settings(tmp_path, analysis_model=ANALYSIS_MODEL))
    assert constructed == [CHAT_MODEL, ANALYSIS_MODEL]
    assert observed == []  # Constructing either role sends no model request.
    with TestClient(app) as client:
        status = client.get("/api/status")
        assert status.status_code == 200
        assert status.json()["model"] == CHAT_MODEL
        assert status.json()["analysis_model"] == ANALYSIS_MODEL
        assert observed == []  # Startup/status use MCP, not model generations.
    await app.state.analysis_provider.analyze({"assignment_id": "fixture", "title": "Fixture", "sources": []})
    await app.state.provider.complete([{"role": "user", "content": "Fixture"}], [])
    assert len(observed) == 2
    assert [(json.loads(request.content)["model"], request.headers["openai-project"])
            for request in observed] == [(ANALYSIS_MODEL, "analysis-folder"), (CHAT_MODEL, "chat-folder")]
    assert all(request.headers["authorization"] == "Api-Key synthetic-role-test-key" for request in observed)
