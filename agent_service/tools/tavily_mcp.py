"""MCP client for Tavily's hosted remote search server.

Course requirement 9 says external services are consumed via the MCP
protocol. Node.js is not available on the demo machine, so the npm
`tavily-mcp` stdio server is not an option; Tavily hosts the same server at
`https://mcp.tavily.com/mcp/` over the Streamable HTTP transport, which the
installed `mcp` Python client speaks natively (docs/MCP.md section 9).

Each search is one short session: initialize, `tools/list`, `tools/call`,
close. The search tool is found by name in the live listing rather than
assumed, and its arguments are limited to what its advertised input schema
accepts, so a renamed tool or argument fails loudly instead of silently
sending a request the server ignores.

The API key travels only in an `Authorization: Bearer` header. Tavily also
accepts it as a `tavilyApiKey` query parameter, but a URL is what HTTP
libraries and the MCP SDK itself write to debug logs and exception messages,
so the key is kept out of it entirely. Every failure message is still
scrubbed of the key before it leaves this module.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import quote

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent, Tool

from agent_service.tools.search_result import SNIPPET_CHARACTER_LIMIT, SearchResult

TAVILY_MCP_URL = "https://mcp.tavily.com/mcp/"
SEARCH_TOOL_NAME = "tavily_search"
CONNECT_TIMEOUT_SECONDS = 15.0
READ_TIMEOUT_SECONDS = 30.0
SECRET_PLACEHOLDER = "***"

RemoteSearch = Callable[[str, str, int], list[SearchResult]]
"""(api_key, query, max_results) -> results; raises on any failure."""


class TavilyMcpProtocolError(RuntimeError):
    """The Tavily MCP server answered in a shape this client does not expect."""


def search_tavily_over_mcp(api_key: str, query: str, max_results: int) -> list[SearchResult]:
    """Run one Tavily search as an MCP tool call (spec section 13, requirement 9).

    Synchronous on purpose: the agent loop is a plain poller, and a search is
    a single request/response, so one `asyncio.run` per call is simpler than
    colouring the loop async (the same choice as `mcp_tools.McpToolClient`).

    Args:
        api_key: The Tavily API key, sent as a bearer header.
        query: The search text.
        max_results: The most results to return.

    Returns:
        The parsed results, at most `max_results` of them.

    Raises:
        Exception: Any transport, protocol or tool failure. Callers that must
            not fail (the search provider) catch it and degrade to `[]`.
    """
    return asyncio.run(_search_async(api_key, query, max_results))


async def _search_async(api_key: str, query: str, max_results: int) -> list[SearchResult]:
    """Open a Streamable HTTP session, find the search tool, call it, close."""
    http_client = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=httpx2.Timeout(CONNECT_TIMEOUT_SECONDS, read=READ_TIMEOUT_SECONDS),
    )
    async with (
        http_client,
        streamable_http_client(TAVILY_MCP_URL, http_client=http_client) as (
            read_stream,
            write_stream,
        ),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        listing = await session.list_tools()
        search_tool = select_search_tool(listing.tools)
        arguments = build_search_arguments(search_tool, query, max_results)
        result = await session.call_tool(
            search_tool.name, arguments, read_timeout_seconds=READ_TIMEOUT_SECONDS
        )

    return parse_search_tool_result(result, max_results)


def select_search_tool(tools: Sequence[Tool]) -> Tool:
    """Pick the web-search tool out of the server's live listing.

    Tavily has published the tool as both `tavily-search` and
    `tavily_search`, so the comparison ignores that difference.

    Args:
        tools: The tools the server advertised.

    Returns:
        The search tool.

    Raises:
        TavilyMcpProtocolError: No advertised tool is the search tool.
    """
    for tool in tools:
        if tool.name.replace("-", "_") == SEARCH_TOOL_NAME:
            return tool

    advertised = ", ".join(tool.name for tool in tools) or "none"
    raise TavilyMcpProtocolError(f"no {SEARCH_TOOL_NAME} tool advertised (got: {advertised})")


def build_search_arguments(tool: Tool, query: str, max_results: int) -> dict[str, Any]:
    """Build the call arguments the search tool's input schema accepts.

    Args:
        tool: The advertised search tool.
        query: The search text.
        max_results: The most results wanted.

    Returns:
        `query`, plus `max_results` when the schema declares it.

    Raises:
        TavilyMcpProtocolError: The schema has no `query` parameter.
    """
    properties = tool.input_schema.get("properties", {})
    if "query" not in properties:
        raise TavilyMcpProtocolError(f"{tool.name} takes no 'query' argument")

    arguments: dict[str, Any] = {"query": query}
    if "max_results" in properties:
        arguments["max_results"] = max_results
    return arguments


def parse_search_tool_result(result: CallToolResult, max_results: int) -> list[SearchResult]:
    """Map a `tavily_search` tool result onto `SearchResult`s.

    The server returns the same JSON object twice: as `structured_content`
    and as a single text block. The structured form is preferred; the text
    block is the fallback for a server that sends only text. A result with
    no URL is dropped, because evidence the agent cites must point at a real
    source (CLAUDE.md R4).

    Args:
        result: The `tools/call` result.
        max_results: The most results to return.

    Returns:
        One `SearchResult` per usable hit, snippets cut to 400 characters.

    Raises:
        TavilyMcpProtocolError: The tool reported an error - either as an
            `is_error` result or, as Tavily does for a bad key, as an
            `error` object inside a normal result - or the payload had no
            `results` list.
    """
    if result.is_error:
        raise TavilyMcpProtocolError(f"search tool reported an error: {_first_text(result)}")

    payload = _payload_from(result)
    if "error" in payload:
        raise TavilyMcpProtocolError(f"search failed inside the tool: {_payload_error(payload)}")

    items = payload.get("results")
    if not isinstance(items, list):
        raise TavilyMcpProtocolError("search payload has no 'results' list")

    parsed = [_search_result_from(item) for item in items if isinstance(item, dict)]
    return [entry for entry in parsed if entry.url][:max_results]


def describe_failure_without_secret(error: BaseException, secret: str) -> str:
    """Describe a failure for logs and traces with the API key removed.

    An exception group from the transport's task group is unwrapped to its
    first leaf, which is the error that actually explains the failure.

    Args:
        error: The failure.
        secret: The API key to scrub, raw and URL-encoded.

    Returns:
        `"<ExceptionType>: <message>"` with every occurrence of the key
        replaced by a placeholder.
    """
    innermost = _innermost_exception(error)
    description = f"{type(innermost).__name__}: {innermost}"
    return scrub_secret(description, secret)


def scrub_secret(text: str, secret: str) -> str:
    """Replace a secret, and its URL-encoded form, with a placeholder.

    Args:
        text: Text that may contain the secret.
        secret: The secret. An empty secret leaves the text unchanged.

    Returns:
        The text with the secret removed.
    """
    if not secret:
        return text
    for form in (secret, quote(secret, safe="")):
        text = text.replace(form, SECRET_PLACEHOLDER)
    return text


def _innermost_exception(error: BaseException) -> BaseException:
    """Follow the first branch of nested exception groups to a leaf."""
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return error


def _payload_from(result: CallToolResult) -> dict[str, Any]:
    """Return the result's JSON object, structured form first."""
    if isinstance(result.structured_content, dict):
        return result.structured_content

    decoded = json.loads(_first_text(result))
    if not isinstance(decoded, dict):
        raise TavilyMcpProtocolError("search payload is not a JSON object")
    return decoded


def _payload_error(payload: dict[str, Any]) -> str:
    """Summarise an in-payload error such as `{"error": ..., "status": 401}`."""
    detail = payload.get("detail")
    reason = detail.get("error") if isinstance(detail, dict) else detail
    return f"{payload.get('error')} (status {payload.get('status')}): {reason}"


def _first_text(result: CallToolResult) -> str:
    """Return the first text block of a tool result, or `""`."""
    for block in result.content:
        if isinstance(block, TextContent):
            return block.text
    return ""


def _search_result_from(item: dict[str, Any]) -> SearchResult:
    """Convert one Tavily hit into the shape the agent cites."""
    return SearchResult(
        title=str(item.get("title") or ""),
        url=str(item.get("url") or ""),
        snippet=str(item.get("content") or "")[:SNIPPET_CHARACTER_LIMIT],
    )
