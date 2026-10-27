"""Catalogue invariants and actual MCP stdio discovery/call/error checks."""

import asyncio
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from studynavigator.catalog import CatalogError, PROJECT_ROOT, Store, seed_database
from studynavigator.mcp_gateway import GatewayError, MCPGateway


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "course.sqlite3"
    seed_database(path)
    return path


def test_seed_is_reproducible_and_keeps_unknown_dates(database):
    before = Store(database).get_assignment("personal-project")
    seed_database(database)
    assert Store(database).get_assignment("personal-project") == before
    assignment = before["assignment"]
    assert assignment["due_date"] is None and assignment["due_at"] is None
    assert assignment["demo"] is False
    assert len(assignment["readme_sections"]) == 9
    facts = {fact["key"]: fact for fact in assignment["facts"]}
    assert facts["lms_links_count"] == {"key": "lms_links_count", "value": 2, "source_id": "personal-project-p1"}
    assert facts["mcp_max_score"]["value"] == 10
    assert facts["max_score"]["source_id"] == "personal-project-p3"
    assert all(Store(database).get_source(fact["source_id"]) for fact in facts.values())


def test_source_contract_and_read_only_database(database):
    store = Store(database)
    source = store.get_source("personal-project-p1")
    assert set(source) == {"source_id", "title", "page", "section", "excerpt", "url"}
    assert source["page"] == 1 and source["url"] == "/sources/personal-project-p1"
    assert "LMS" in source["excerpt"] and "README.md" in source["excerpt"]
    connection = store._connect()
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM assignments")
    finally:
        connection.close()
    assert store.get_source("missing") is None


def test_fts_search_filters_and_parameterizes_user_text(database):
    store = Store(database)
    matches = store.search_materials("Что отправить в LMS?", assignment_id="personal-project")["matches"]
    assert any(item["source_id"] == "personal-project-p1" for item in matches)
    assert store.search_materials("LMS", assignment_id="different")["matches"] == []
    assert store.search_materials("quuxnonexistentzzzz")["matches"] == []
    store.search_materials("LMS'; DROP TABLE assignments; --")
    store.search_materials('"OR NOT ( : *')
    assert len(store.list_assignments()["assignments"]) == 1


@pytest.mark.asyncio
async def test_real_stdio_discovery_calls_and_error(database):
    gateway = MCPGateway(database)
    async with gateway:
        assert {tool["name"] for tool in gateway.tools} == {"list_assignments", "get_assignment", "search_materials"}
        assert all(tool["inputSchema"]["additionalProperties"] is False for tool in gateway.tools)
        listed = await gateway.call("list_assignments", {})
        assert listed["assignments"][0]["assignment_id"] == "personal-project"
        result = await gateway.call("get_assignment", {"assignment_id": "personal-project"})
        assert result["assignment"]["due_date"] is None
        found = await gateway.call("search_materials", {"query": "MCP баллы", "limit": 5, "assignment_id": "personal-project"})
        assert any(source["source_id"] == "personal-project-mcp-p2" for source in found["matches"])
        empty = await gateway.call("search_materials", {"query": "quuxnonexistentzzzz"})
        assert empty == {"matches": []}
        with pytest.raises(GatewayError) as error:
            await gateway.call("get_assignment", {"assignment_id": "missing"})
        assert error.value.code == "unknown_assignment"
    assert gateway._client is None
    assert gateway.trace[-1]["status"] == "unknown_assignment"
    assert all(item["duration_ms"] >= 0 for item in gateway.trace)


@pytest.mark.asyncio
async def test_server_validation_and_is_error_really_cross_protocol(database):
    parameters = StdioServerParameters(command=sys.executable,
        args=["-m", "studynavigator.mcp_server", "--db", str(database)], cwd=PROJECT_ROOT)
    async with Client(parameters, cache=None) as client:
        result = await client.call_tool("search_materials", {"query": "MCP", "limit": True})
        assert result.is_error is True
        result = await client.call_tool("get_assignment", {"assignment_id": "missing"})
        assert result.is_error is True
        assert "unknown_assignment" in " ".join(getattr(item, "text", "") for item in result.content)


@pytest.mark.asyncio
async def test_caller_error_preserved_after_real_stdio_cleanup(database):
    gateway = MCPGateway(database)
    with pytest.raises(ValueError, match="caller error"):
        async with gateway:
            raise ValueError("caller error")
    assert gateway._client is None and gateway._client_context is None


class CountingClient:
    def __init__(self, result=None, delay=0):
        self.calls = 0
        self.result = result or SimpleNamespace(is_error=False, structured_content={"matches": []}, content=[])
        self.delay = delay

    async def call_tool(self, name, arguments):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.result


@pytest.mark.asyncio
@pytest.mark.parametrize("name,arguments", [
    ("write_grades", {}),
    ("get_assignment", {"assignment_id": "personal-project", "sql": "SELECT 1"}),
    ("get_assignment", {"assignment_id": ""}),
    ("search_materials", {"query": "MCP", "limit": True}),
    ("search_materials", {"query": "MCP", "limit": 11}),
    ("search_materials", {"query": " "}),
    ("search_materials", {"query": "a" * 501}),
])
async def test_invalid_arguments_never_dispatch(database, name, arguments):
    gateway = MCPGateway(database)
    fake = CountingClient()
    gateway._client = fake
    with pytest.raises(GatewayError):
        await gateway.call(name, arguments)
    assert fake.calls == 0


@pytest.mark.asyncio
async def test_disconnected_timeout_and_malformed_result_are_not_empty_success(database):
    gateway = MCPGateway(database, timeout=0.01)
    with pytest.raises(GatewayError) as error:
        await gateway.call("list_assignments", {})
    assert error.value.code == "transport_unavailable"
    gateway._client = CountingClient(delay=1)
    with pytest.raises(GatewayError) as error:
        await gateway.call("search_materials", {"query": "MCP"})
    assert error.value.code == "timeout"
    gateway._client = CountingClient(SimpleNamespace(is_error=False, structured_content={"wrong": []}, content=[]))
    with pytest.raises(GatewayError) as error:
        await gateway.call("search_materials", {"query": "MCP"})
    assert error.value.code == "invalid_result"


@pytest.mark.asyncio
async def test_request_cancellation_does_not_destroy_shared_client(database):
    gateway = MCPGateway(database)
    fake = CountingClient(delay=1)
    gateway._client = fake
    task = asyncio.create_task(gateway.call("search_materials", {"query": "MCP"}))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert gateway._client is fake
    assert gateway.trace[-1]["status"] == "cancelled"
    fake.delay = 0
    assert await gateway.call("search_materials", {"query": "MCP"}) == {"matches": []}
