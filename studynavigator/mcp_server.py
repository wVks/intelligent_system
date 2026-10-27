"""Own MCP server. Run with ``python -m studynavigator.mcp_server --db PATH``."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, StrictInt

from .catalog import CatalogError, Store

Identifier = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")]
Query = Annotated[str, Field(min_length=1, max_length=500)]
ResultLimit = Annotated[StrictInt, Field(ge=1, le=10)]


class AssignmentList(BaseModel):
    assignments: list[dict[str, Any]]


class AssignmentResult(BaseModel):
    assignment: dict[str, Any]


class SearchResult(BaseModel):
    matches: list[dict[str, Any]]


def create_server(db_path: Path, workspace_id: str | None = None) -> MCPServer:
    # Trusted backend context, never a model-supplied tool argument.
    store = Store(db_path, workspace_id=workspace_id)
    server = MCPServer("StudyNavigator", version="0.1.0", log_level="WARNING",
                       instructions="Read course sources before answering. Missing deadlines remain unknown.")
    annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    @server.tool(annotations=annotations)
    def list_assignments(course_id: Identifier | None = None) -> AssignmentList:
        """Получить учебные задания и известные сроки курса."""
        return AssignmentList(**store.list_assignments(course_id))

    @server.tool(annotations=annotations)
    def get_assignment(assignment_id: Identifier) -> AssignmentResult:
        """Получить требования, критерии, факты и подтверждающие источники задания."""
        try:
            return AssignmentResult(**store.get_assignment(assignment_id))
        except CatalogError as exc:
            raise ToolError(f"{exc.code}: {exc.message}") from exc

    @server.tool(annotations=annotations)
    def search_materials(query: Query, limit: ResultLimit = 5,
                         assignment_id: Identifier | None = None) -> SearchResult:
        """Найти подтверждающие фрагменты материалов; ограничить выбранным заданием."""
        try:
            return SearchResult(**store.search_materials(query, limit, assignment_id))
        except CatalogError as exc:
            raise ToolError(f"{exc.code}: {exc.message}") from exc

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--workspace", default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)  # stderr; stdout belongs to MCP.
    create_server(args.db, workspace_id=args.workspace).run(transport="stdio")


if __name__ == "__main__":
    main()
