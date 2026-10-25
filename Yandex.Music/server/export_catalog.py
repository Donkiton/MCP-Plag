"""Regenerate discovery metadata after changing the existing runtime/tools."""
import asyncio
import json
from pathlib import Path

import runtime
import mcp.types as types
from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS


async def collect():
    server = runtime.upstream.mcp
    options = server._mcp_server.create_initialization_options()
    initialize = types.InitializeResult(
        protocolVersion=types.LATEST_PROTOCOL_VERSION, capabilities=options.capabilities,
        serverInfo=types.Implementation(name=options.server_name, version=options.server_version),
        instructions=options.instructions)
    def dump(value):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    return {"initialize": dump(initialize), "protocols": SUPPORTED_PROTOCOL_VERSIONS,
            "tools/list": {"tools": [dump(t) for t in await server.list_tools()]},
            "resources/list": {"resources": [dump(t) for t in await server.list_resources()]},
            "resources/templates/list": {"resourceTemplates": [dump(t) for t in await server.list_resource_templates()]},
            "prompts/list": {"prompts": [dump(t) for t in await server.list_prompts()]}}


if __name__ == "__main__":
    Path(__file__).with_name("music_catalog.json").write_text(
        json.dumps(asyncio.run(collect()), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
