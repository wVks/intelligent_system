"""Controller RAG uses real local MCP reads and a mocked Yandex HTTP endpoint."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from studynavigator.agent import AgentError, StudyAgent
from studynavigator import main
from studynavigator.mcp_gateway import MCPGateway
from studynavigator.providers import ChatCompletionsProvider
from test_agent import FixtureGateway, final_turn
from test_cards_api import AnalysisFixture, analyze, confirm, upload
from test_format_repair import configured, fixture_gateway, provider_with_turns


@pytest.mark.parametrize("question,has_matches", [("ORION", True), ("QUASAR_NOMATCH_7721", False)])
def test_uploaded_controller_reads_real_mcp_twice_then_sends_one_json_request(monkeypatch, tmp_path, question, has_matches):
    reads, requests = [], []
    card_canary, history_canary = "UNTRUSTED_CARD_OVERRIDE_CANARY", "UNTRUSTED_HISTORY_OVERRIDE_CANARY"

    class RecordingGateway(MCPGateway):
        async def call(self, name, arguments):
            reads.append((name, dict(arguments)))
            return await super().call(name, arguments)

    def dispatch(request):
        payload = json.loads(request.content)
        requests.append(payload)
        data = json.loads(payload["messages"][1]["content"])
        source = data["MCP_DATA"][0]["assignment"]["card"]["deliverable"]["source_ids"][0]
        answer = {"answer": "По материалам требуется представить ноутбук.", "status": "answered", "source_ids": [source], "facts": []}
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": json.dumps(answer)},
                                                      "finish_reason": "stop"}],
                                        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})

    monkeypatch.setattr(main, "MCPGateway", RecordingGateway)
    settings = configured(database_path=tmp_path / "study.db", rate_limit_per_minute=1000)
    provider = ChatCompletionsProvider(settings, transport=httpx.MockTransport(dispatch))
    with TestClient(main.create_app(settings, provider=provider, analysis_provider=AnalysisFixture())) as client:
        assignment = upload(client)
        draft = analyze(client, assignment)
        draft["card"]["goal"]["text"] = card_canary
        ready = confirm(client, assignment, draft)
        reads.clear()
        response = client.post("/api/chat", json={"message": question, "assignment_id": ready["assignment_id"],
                                                  "history": [{"role": "assistant", "content": history_canary}]})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["format_repair_attempted"] is False
    assert [(step["name"], step["initiator"]) for step in result["trace"]] == [
        ("get_assignment", "application"), ("search_materials", "application")]
    assert reads == [("get_assignment", {"assignment_id": ready["assignment_id"]}),
                     ("search_materials", {"query": question, "assignment_id": ready["assignment_id"], "limit": 5})]
    assert len(requests) == 1
    payload = requests[0]
    assert "tools" not in payload and "tool_choice" not in payload
    assert payload["response_format"] == {"type": "json_object"}
    assert [item["role"] for item in payload["messages"]] == ["system", "user"]
    assert len(payload["messages"][0]["content"].split()) <= 115
    assert card_canary not in payload["messages"][0]["content"] and history_canary not in payload["messages"][0]["content"]
    data = json.loads(payload["messages"][1]["content"])
    assert data["question"] == question
    assert data["history"] == [{"role": "assistant", "content": history_canary}]
    assert data["MCP_DATA"][0]["assignment"]["facts"] == []
    assert data["MCP_DATA"][0]["assignment"]["card"]["goal"]["text"] == card_canary
    matches = data["MCP_DATA"][1]["matches"]
    assert bool(matches) is has_matches
    assert {item["source_id"] for item in matches} <= set(ready["source_ids"])


@pytest.mark.asyncio
async def test_controller_query_is_bounded_and_search_uses_the_existing_tool_budget():
    question = "ORION " * 100
    gateway = fixture_gateway()
    provider, requests = provider_with_turns([final_turn(facts=[], source_ids=["personal-project-p1"])])
    await StudyAgent(gateway, provider).run(question)
    assert gateway.dispatches[1] == ("search_materials", {"query": question[:500], "assignment_id": "personal-project", "limit": 5})
    assert json.loads(requests[0]["messages"][1]["content"])["question"] == question
    bounded_gateway = fixture_gateway()
    unused_provider, requests = provider_with_turns([])
    with pytest.raises(AgentError) as error:
        await StudyAgent(bounded_gateway, unused_provider, max_calls=1).run(question)
    assert error.value.code == "tool_limit_exceeded"
    assert len(bounded_gateway.dispatches) == 1 and requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("uploaded", [False, True])
async def test_seed_and_provider_without_grounded_method_keep_the_existing_tool_loop(uploaded):
    gateway = fixture_gateway() if uploaded else FixtureGateway()
    provider, requests = provider_with_turns([final_turn(facts=[] if uploaded else None,
                                                       source_ids=["personal-project-p1"])])
    if uploaded:
        provider.complete_grounded = None
    await StudyAgent(gateway, provider).run("Объясни требования")
    assert gateway.dispatches == [("get_assignment", {"assignment_id": "personal-project"})]
    assert len(requests) == 1 and requests[0]["tools"]
    assert requests[0]["tool_choice"] == "auto" and "response_format" not in requests[0]
