"""Run real stdio MCP discovery/calls against an isolated seeded database."""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studynavigator.catalog import seed_database
from studynavigator.mcp_gateway import GatewayError, MCPGateway


async def main():
    with tempfile.TemporaryDirectory(prefix="studynavigator-mcp-") as temporary:
        database = Path(temporary) / "course.sqlite3"
        seed_database(database)
        async with MCPGateway(database) as gateway:
            assignment = await gateway.call("get_assignment", {"assignment_id": "personal-project"})
            found = await gateway.call("search_materials", {"query": "Что отправить в LMS?", "assignment_id": "personal-project"})
            await gateway.call("list_assignments", {})
            try:
                await gateway.call("get_assignment", {"assignment_id": "missing"})
            except GatewayError as error:
                assert error.code == "unknown_assignment"
            else:
                raise AssertionError("Unknown assignment must fail")
            print(json.dumps({"mode": "real_mcp_stdio", "tools": [tool["name"] for tool in gateway.tools],
                "assignment": assignment["assignment"]["assignment_id"],
                "matching_sources": [source["source_id"] for source in found["matches"]],
                "trace": gateway.trace, "llm_tested": False}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
