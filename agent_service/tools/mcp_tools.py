"""Client wrapper around the local MCP tool server.

The agent obtains adopter and animal records exclusively through these tools,
never by importing the Flask application (rule R2). The server runs as a
separate subprocess and speaks MCP over stdio; see docs/MCP.md.

The MCP client API is asynchronous, while the agent loop is a plain
synchronous poller. Rather than colour the whole agent async for two calls,
this module runs each session in `asyncio.run`. Sessions are short-lived by
design: a tool call is a request/response, and holding a subprocess open
across a long analysis would add failure modes for no benefit.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ContentBlock, TextContent

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ToolCallRecord:
    """A record of one tool invocation, for the agent's reasoning trace."""

    tool_name: str
    arguments: dict[str, Any]
    succeeded: bool
    summary: str


class ProfileLookup(Protocol):
    """The record-fetching surface the agent depends on.

    Declared as a protocol so the agent depends on *what it needs* - two
    lookups - rather than on a class that spawns a subprocess. Rule R2
    still holds either way: the only implementation that reaches real data
    is the MCP client below.
    """

    def get_adopter_profile(self, adopter_profile_id: str) -> dict[str, Any]:
        """Fetch one adopter profile."""
        ...

    def get_animal_profile(self, animal_id: str) -> dict[str, Any]:
        """Fetch one animal profile."""
        ...


class McpToolClient:
    """Synchronous access to the PetMatch MCP tools."""

    def __init__(self, python_executable: str | None = None) -> None:
        """Configure how the tool server subprocess is launched."""
        self._python_executable = python_executable or sys.executable
        self.call_history: list[ToolCallRecord] = []

    def get_adopter_profile(self, adopter_profile_id: str) -> dict[str, Any]:
        """Fetch one adopter profile through the MCP server.

        Args:
            adopter_profile_id: The adopter profile's UUID.

        Returns:
            The tool's payload. A missing record yields `{"found": False, ...}`
            rather than raising, so the agent can record it as missing
            information and carry on.
        """
        return self._call_tool("get_adopter_profile", {"adopter_profile_id": adopter_profile_id})

    def get_animal_profile(self, animal_id: str) -> dict[str, Any]:
        """Fetch one animal profile through the MCP server.

        Args:
            animal_id: The animal's UUID.

        Returns:
            The tool's payload, or `{"found": False, ...}` when absent.
        """
        return self._call_tool("get_animal_profile", {"animal_id": animal_id})

    def list_available_tools(self) -> list[str]:
        """Return the names the server advertises."""
        return asyncio.run(self._list_tools_async())

    def _call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke one tool and record the outcome."""
        try:
            payload = asyncio.run(self._call_tool_async(tool_name, arguments))
        except Exception as error:  # - a tool failure must not end the loop
            self.call_history.append(
                ToolCallRecord(tool_name, arguments, False, f"{type(error).__name__}: {error}")
            )
            return {"found": False, "reason": f"Tool {tool_name} failed: {error}"}

        self.call_history.append(
            ToolCallRecord(
                tool_name,
                arguments,
                bool(payload.get("found")),
                "record returned" if payload.get("found") else "no such record",
            )
        )
        return payload

    async def _call_tool_async(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Open a session, call one tool, and close."""
        async with (
            stdio_client(self._server_parameters()) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)

        return _payload_from(tool_name, result.content)

    async def _list_tools_async(self) -> list[str]:
        """Open a session and list the advertised tools."""
        async with (
            stdio_client(self._server_parameters()) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            listing = await session.list_tools()

        return [tool.name for tool in listing.tools]

    def _server_parameters(self) -> StdioServerParameters:
        """Describe how to spawn the tool server."""
        return StdioServerParameters(
            command=self._python_executable,
            args=["-m", "mcp_server"],
            cwd=str(PROJECT_ROOT),
        )


class ToolProtocolError(RuntimeError):
    """An MCP tool answered with something other than a JSON text block."""


def _payload_from(tool_name: str, content: Sequence[ContentBlock]) -> dict[str, Any]:
    """Read a tool's JSON payload out of its reply.

    A tool reply is a list of content blocks, any of which may be an image,
    an audio clip or an embedded resource. Both PetMatch tools answer with a
    single JSON text block, so anything else means the server and this
    client disagree about the contract - which is worth failing loudly for
    rather than reading an attribute that may not exist.

    Args:
        tool_name: The tool that replied, named in the error.
        content: The reply's content blocks.

    Returns:
        The decoded payload.

    Raises:
        ToolProtocolError: The reply was empty, was not text, or did not
            decode to a JSON object.
    """
    if not content:
        raise ToolProtocolError(f"{tool_name} returned no content")

    first_block = content[0]
    if not isinstance(first_block, TextContent):
        raise ToolProtocolError(
            f"{tool_name} returned {type(first_block).__name__}, expected text"
        )

    payload = json.loads(first_block.text)
    if not isinstance(payload, dict):
        raise ToolProtocolError(
            f"{tool_name} returned {type(payload).__name__}, expected an object"
        )

    return payload
