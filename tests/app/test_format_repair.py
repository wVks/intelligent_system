"""Bounded JSON repair over mocked HTTP; no paid APIs or model-quality claims."""

import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from studynavigator.agent import AgentError, StudyAgent
from studynavigator.config import Settings
from studynavigator.main import create_app
from studynavigator.providers import ChatCompletionsProvider
from test_agent import FixtureGateway, final_turn, tool_turn
from test_cards_api import AnalysisFixture, analyze, confirm, upload


INVALID_CANARY = "PRIVATE_INVALID_RAW_RESPONSE_CANARY {"


def configured(**overrides):
    return Settings(**({"mode": "live", "api_base_url": "https://ai.api.cloud.yandex.net/v1",
                        "model": "gpt://fixture-folder/yandexgpt-5.1/latest", "api_key": "synthetic-repair-key"} | overrides))


def fixture_gateway(**overrides):
    gateway = FixtureGateway()
    gateway.assignment.update({"is_uploaded": True, "facts": [], "card": {
        "goal": {"text": "Explain the fixture assignment", "origin": "document", "source_ids": ["personal-project-p1"]}}} | overrides)
    return gateway


def provider_with_turns(turns, **overrides):
    observed = []

    def dispatch(request):
        observed.append(json.loads(request.content))
        index = len(observed) - 1
        assert index < len(turns), "An unexpected extra model request was dispatched"
        return httpx.Response(200, json={"choices": [{"message": turns[index], "finish_reason": "stop"}],
                                        "usage": {"prompt_tokens": 100 * (index + 1), "completion_tokens": 10 * (index + 1),
                                                  "total_tokens": 110 * (index + 1)}})

    return ChatCompletionsProvider(configured(**overrides), transport=httpx.MockTransport(dispatch)), observed


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_cap", [1000, 100])
async def test_invalid_uploaded_final_gets_one_tools_free_json_repair_and_combined_usage(caplog, chat_cap):
    provider, observed = provider_with_turns([
        {"role": "assistant", "content": INVALID_CANARY},
        final_turn(facts=[], source_ids=["personal-project-p1"])], chat_max_tokens=chat_cap)
    with caplog.at_level(logging.WARNING, logger="studynavigator"):
        result = await StudyAgent(fixture_gateway(), provider).run("Объясни требования")
    assert result["format_repair_attempted"] is True
    assert result["facts"] == []
    assert result["usage"] == {"prompt_tokens": 300, "completion_tokens": 30, "total_tokens": 330}
    assert len(observed) == 2
    initial, repair = observed
    assert initial["tools"] and initial["tool_choice"] == "auto" and "response_format" not in initial
    assert "tools" not in repair and "tool_choice" not in repair
    assert repair["response_format"] == {"type": "json_object"}
    assert repair["max_tokens"] == min(chat_cap, 250)
    assert repair["messages"][1:] == initial["messages"][1:]
    assert repair["messages"][0]["content"].startswith(initial["messages"][0]["content"])
    instruction = repair["messages"][0]["content"][len(initial["messages"][0]["content"]):]
    assert len(instruction.split()) <= 40 and "до 40 слов" in instruction
    assert any(item["role"] == "tool" and json.loads(item["content"])["assignment"]["facts"] == []
               for item in repair["messages"])
    assert INVALID_CANARY not in json.dumps(repair) and INVALID_CANARY not in caplog.text


@pytest.mark.asyncio
async def test_valid_final_does_not_trigger_format_repair():
    provider, observed = provider_with_turns([final_turn(facts=[], source_ids=["personal-project-p1"])])
    result = await StudyAgent(fixture_gateway(), provider).run("Объясни требования")
    assert result["format_repair_attempted"] is False
    assert len(observed) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("turn,code", [
    (final_turn(facts=[{"key": "invented", "value": 1, "source_id": "personal-project-p1"}]), "unsupported_fact"),
    (final_turn(facts=[], source_ids=["unknown-source"]), "unsupported_source"),
    (final_turn(facts=[], source_ids=[]), "missing_sources"),
])
async def test_semantic_failure_never_triggers_format_repair(turn, code):
    provider, observed = provider_with_turns([turn])
    with pytest.raises(AgentError) as caught:
        await StudyAgent(fixture_gateway(), provider).run("Объясни требования")
    assert caught.value.code == code
    assert len(observed) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("turn,code", [
    (final_turn(facts=[{"key": "invented", "value": 1, "source_id": "personal-project-p1"}]), "unsupported_fact"),
    (final_turn(facts=[], source_ids=["unknown-source"]), "unsupported_source"),
])
async def test_repaired_json_still_has_to_pass_the_same_source_and_fact_checks(turn, code):
    provider, observed = provider_with_turns([{"role": "assistant", "content": INVALID_CANARY}, turn])
    with pytest.raises(AgentError) as caught:
        await StudyAgent(fixture_gateway(), provider).run("Объясни требования")
    assert caught.value.code == code
    assert len(observed) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [{"is_uploaded": False}, {"facts": [{"key": "known", "value": 1, "source_id": "personal-project-p1"}]}])
async def test_seed_or_nonempty_fact_registry_does_not_enable_repair(overrides):
    provider, observed = provider_with_turns([{"role": "assistant", "content": INVALID_CANARY}])
    with pytest.raises(AgentError) as caught:
        await StudyAgent(fixture_gateway(**overrides), provider).run("Объясни требования")
    assert caught.value.code == "invalid_model_response"
    assert len(observed) == 1


@pytest.mark.asyncio
async def test_generic_provider_does_not_enable_yandex_format_repair():
    provider, observed = provider_with_turns([tool_turn(), {"role": "assistant", "content": INVALID_CANARY}],
                                             api_base_url="https://provider.example/v1", model="fixture-model")
    with pytest.raises(AgentError) as caught:
        await StudyAgent(fixture_gateway(), provider).run("Объясни требования")
    assert caught.value.code == "invalid_model_response"
    assert len(observed) == 2 and all("response_format" not in item for item in observed)


def test_second_invalid_final_still_returns_503_after_exactly_one_repair(tmp_path, caplog):
    provider, observed = provider_with_turns([{"role": "assistant", "content": INVALID_CANARY}] * 2)
    app = create_app(Settings(database_path=tmp_path / "study.db", rate_limit_per_minute=1000),
                     provider=provider, analysis_provider=AnalysisFixture())
    with caplog.at_level(logging.WARNING, logger="studynavigator"), TestClient(app) as client:
        assignment = upload(client)
        ready = confirm(client, assignment, analyze(client, assignment))
        response = client.post("/api/chat", json={"message": "Объясни требования", "assignment_id": ready["assignment_id"]})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "invalid_model_response"
    assert len(observed) == 2
    assert INVALID_CANARY not in caplog.text
