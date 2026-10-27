"""Validated MCP dispatcher using an actual SDK stdio connection."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from .catalog import PROJECT_ROOT
from .mcp_server import Identifier, Query, ResultLimit


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListArguments(ToolArguments):
    course_id: Identifier | None = None


class GetArguments(ToolArguments):
    assignment_id: Identifier


class SearchArguments(ToolArguments):
    query: Query
    limit: ResultLimit = 5
    assignment_id: Identifier | None = None

    @field_validator("query")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value


ARGUMENT_MODELS = {"list_assignments": ListArguments, "get_assignment": GetArguments,
                   "search_materials": SearchArguments}


class GatewayError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


class MCPGateway:
    def __init__(self, db_path: Path, timeout: float = 10, *, workspace_id: str | None = None):
        self.db_path = Path(db_path).resolve()
        self.timeout = timeout
        self.workspace_id = workspace_id
        self.tools: list[dict[str, Any]] = []
        self.trace: list[dict[str, Any]] = []
        self._client: Client | None = None
        self._client_context: Client | None = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "MCPGateway":
        arguments = ["-m", "studynavigator.mcp_server", "--db", str(self.db_path)]
        if self.workspace_id is not None:
            arguments.extend(["--workspace", self.workspace_id])
        parameters = StdioServerParameters(command=sys.executable,
            args=arguments,
            cwd=PROJECT_ROOT, env={**os.environ, "PYTHONUNBUFFERED": "1"})
        client = Client(parameters, read_timeout_seconds=self.timeout, cache=None)
        try:
            # Enter/exit in the owning lifespan task, not a wait_for-created task.
            self._client = await client.__aenter__()
            self._client_context = client
            async with asyncio.timeout(self.timeout):
                discovered = await self._client.list_tools()
            names = {tool.name for tool in discovered.tools}
            if not set(ARGUMENT_MODELS).issubset(names):
                raise GatewayError("invalid_catalog", "MCP-сервер не предоставил обязательные инструменты.")
            self.tools = [{"name": tool.name, "description": tool.description or "",
                           "inputSchema": ARGUMENT_MODELS[tool.name].model_json_schema()}
                          for tool in discovered.tools if tool.name in ARGUMENT_MODELS]
            return self
        except BaseException as exc:
            if self._client_context is not None:
                # Our context owns propagation; injecting the caller's exception
                # into SDK task groups would wrap it in an ExceptionGroup.
                await self._client_context.__aexit__(None, None, None)
            self._client = self._client_context = None
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit, GatewayError)):
                raise
            raise GatewayError("transport_unavailable", "Не удалось подключиться к учебным материалам через MCP.") from exc

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        try:
            if self._client_context is not None:
                await self._client_context.__aexit__(None, None, None)
        finally:
            self._client = self._client_context = None

    @staticmethod
    def validate_arguments(name: str, arguments: Any) -> dict[str, Any]:
        model = ARGUMENT_MODELS.get(name)
        if model is None:
            raise GatewayError("unknown_tool", "Запрошен недоступный инструмент.")
        if not isinstance(arguments, dict):
            raise GatewayError("invalid_arguments", "Аргументы инструмента должны быть объектом.")
        try:
            return model.model_validate(arguments).model_dump(exclude_none=True)
        except ValidationError as exc:
            raise GatewayError("invalid_arguments", "Некорректные параметры учебного инструмента.") from exc

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        status = "ok"
        try:
            validated = self.validate_arguments(name, arguments)
            if self._client is None:
                raise GatewayError("transport_unavailable", "Учебный источник сейчас недоступен.")
            async with asyncio.timeout(self.timeout):
                async with self._lock:
                    result = await self._client.call_tool(name, validated)
            if result.is_error:
                message = " ".join(getattr(content, "text", "") for content in result.content)
                code = "unknown_assignment" if "unknown_assignment:" in message else "tool_error"
                raise GatewayError(code, "Учебное задание не найдено." if code == "unknown_assignment" else "Учебный инструмент завершился ошибкой.")
            payload = result.structured_content
            if payload is None:
                for content in result.content:
                    if getattr(content, "type", None) == "text":
                        try:
                            payload = json.loads(content.text)
                            break
                        except (ValueError, TypeError):
                            continue
            key = {"list_assignments": "assignments", "get_assignment": "assignment", "search_materials": "matches"}[name]
            expected = dict if name == "get_assignment" else list
            if not isinstance(payload, dict) or key not in payload or not isinstance(payload[key], expected):
                raise GatewayError("invalid_result", "MCP-сервер вернул некорректный результат.")
            return payload
        except asyncio.TimeoutError as exc:
            status = "timeout"
            raise GatewayError("timeout", "Учебный источник не ответил вовремя.") from exc
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except GatewayError as exc:
            status = exc.code
            raise
        except Exception as exc:
            status = "transport_unavailable"
            raise GatewayError(status, "Соединение с учебным источником прервано.") from exc
        finally:
            self.trace.append({"name": name, "status": status,
                               "duration_ms": round((time.monotonic() - started) * 1000, 2)})
