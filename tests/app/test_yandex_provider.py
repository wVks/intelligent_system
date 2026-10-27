"""Yandex compatibility is verified without credentials or network requests."""

import json

import httpx
import pytest

from studynavigator.config import Settings
from studynavigator.providers import ChatCompletionsProvider


def configured(**overrides):
    values = {"mode": "live", "api_base_url": "https://ai.api.cloud.yandex.net/v1",
              "model": "gpt://fixture-folder/yandexgpt-5.1/latest", "api_key": "synthetic-key"}
    return Settings(**(values | overrides))


def response(content, usage=None):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content},
                                                 "finish_reason": "stop"}], "usage": usage})


@pytest.mark.asyncio
async def test_yandex_auth_project_auto_tools_and_bounded_chat_response():
    observed = []
    usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}

    def handler(request):
        observed.append(request)
        return response("Ответ по материалам", usage)

    provider = ChatCompletionsProvider(configured(chat_max_tokens=450), transport=httpx.MockTransport(handler))
    messages = [{"role": "user", "content": "Что сдать?"}]
    tools = [{"type": "function", "function": {"name": "get_assignment", "parameters": {"type": "object"}}}]
    result = await provider.complete(messages, tools, require_tool=True)
    request = observed[0]
    payload = json.loads(request.content)
    assert str(request.url) == "https://ai.api.cloud.yandex.net/v1/chat/completions"
    assert request.headers["authorization"] == "Api-Key synthetic-key"
    assert request.headers["openai-project"] == "fixture-folder"
    assert provider.preload_assignment is True
    assert payload["model"] == "gpt://fixture-folder/yandexgpt-5.1/latest"
    assert payload["messages"] == messages and payload["tools"] == tools
    assert payload["tool_choice"] == "auto" and payload["max_tokens"] == 450
    assert "response_format" not in payload
    assert "synthetic-key" not in request.content.decode()
    assert result["_usage"] == usage


@pytest.mark.asyncio
async def test_yandex_analysis_uses_separate_json_request_and_configured_output_limit():
    observed = []

    def handler(request):
        observed.append(json.loads(request.content))
        return response('{"title":"Учебное задание"}')

    provider = ChatCompletionsProvider(configured(analysis_max_tokens=900), transport=httpx.MockTransport(handler))
    result = await provider.analyze({"assignment_id": "upload-fixture", "title": "Учебное задание", "sources": [
        {"source_id": "fixture-page-1", "page": 1, "excerpt": "Сдать отчёт."}]})
    payload = observed[0]
    assert payload["max_tokens"] == 900
    assert payload["response_format"] == {"type": "json_object"}
    assert "tools" not in payload and "tool_choice" not in payload
    assert result["card"]["title"] == "Учебное задание"


@pytest.mark.parametrize("model", ["yandexgpt-5.1", "gpt:///yandexgpt-5.1", "gpt://folder/",
                                   "gpt://folder:123/model", "gpt://folder/model?token=value"])
def test_yandex_requires_an_unambiguous_folder_in_model_uri(model):
    with pytest.raises(ValueError, match="Yandex API requires"):
        ChatCompletionsProvider(configured(model=model))


@pytest.mark.asyncio
async def test_other_api_hosts_keep_bearer_auth_and_forced_tool_selection():
    observed = []

    def handler(request):
        observed.append(request)
        return response("Обычный провайдер")

    provider = ChatCompletionsProvider(configured(api_base_url="https://ai.api.cloud.yandex.net.example/v1",
                                                model="fixture-model"), transport=httpx.MockTransport(handler))
    await provider.complete([], [], require_tool=True)
    request = observed[0]
    assert provider.preload_assignment is False
    assert request.headers["authorization"] == "Bearer synthetic-key"
    assert "openai-project" not in request.headers
    assert json.loads(request.content)["tool_choice"] == {"type": "function", "function": {"name": "get_assignment"}}


@pytest.mark.parametrize("field", ["chat_max_tokens", "analysis_max_tokens"])
@pytest.mark.parametrize("value", [0, -1, 1.5, True])
def test_output_limits_reject_unbounded_or_non_integer_values(field, value):
    with pytest.raises(ValueError, match="positive integer"):
        Settings(**{field: value})


def test_output_limits_load_from_environment(monkeypatch):
    monkeypatch.setenv("STUDYNAV_CHAT_MAX_TOKENS", "350")
    monkeypatch.setenv("STUDYNAV_ANALYSIS_MAX_TOKENS", "1200")
    settings = Settings.from_env()
    assert settings.chat_max_tokens == 350 and settings.analysis_max_tokens == 1200
    assert Settings().chat_max_tokens == 1000 and Settings().analysis_max_tokens == 3000
