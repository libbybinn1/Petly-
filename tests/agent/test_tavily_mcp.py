"""Tests for consuming Tavily web search through its remote MCP server.

Course requirement 9: external services are consumed via MCP. Everything
here is offline. Tool results are recorded shapes of what the live
`tavily_search` tool returned on 2026-10-25, and the one test that drives the
real Streamable HTTP client answers it from an in-process mock transport.

Per CLAUDE.md R3 nothing asserts on result wording: the assertions are about
structure, limits, failure degradation and keeping the API key secret.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx2
import pytest
from agent_service.tools import tavily_mcp
from agent_service.tools.search_result import SNIPPET_CHARACTER_LIMIT, SearchResult
from agent_service.tools.tavily_mcp import (
    TavilyMcpProtocolError,
    build_search_arguments,
    describe_failure_without_secret,
    parse_search_tool_result,
    scrub_secret,
    search_tavily_over_mcp,
    select_search_tool,
)
from agent_service.tools.web_search import (
    StubSearchProvider,
    TavilyMcpSearchProvider,
    build_search_provider,
)
from mcp.types import CallToolResult, TextContent, Tool

pytestmark = pytest.mark.agent

FAKE_API_KEY = "tvly-dev-FAKE+key/with=reserved&chars"

# The input schema the live server advertised for `tavily_search`, trimmed
# to the parameters this client reads.
RECORDED_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Search query"},
        "max_results": {"type": "integer", "default": 5},
        "search_depth": {"type": "string", "default": "basic"},
    },
    "required": ["query"],
}

RECORDED_PAYLOAD: dict[str, Any] = {
    "query": "dog adoption first week tips",
    "follow_up_questions": None,
    "answer": None,
    "images": [],
    "results": [
        {
            "url": "https://example.org/first-week.pdf",
            "title": "The First 7 Days",
            "content": "x" * 900,
            "score": 0.82,
            "raw_content": None,
            "id": "result-1",
        },
        {
            "url": "https://example.org/settling-in",
            "title": "Settling in",
            "content": "Short snippet.",
            "score": 0.71,
            "raw_content": None,
            "id": "result-2",
        },
    ],
    "response_time": 0.9,
    "request_id": "request-1",
}


def make_tool(name: str, schema: dict[str, Any] | None = None) -> Tool:
    """Build an advertised tool as the server's `tools/list` returns it."""
    return Tool(name=name, input_schema=schema or RECORDED_SEARCH_SCHEMA)


def recorded_result(payload: dict[str, Any], *, structured: bool = True) -> CallToolResult:
    """Build a `tools/call` result in the shape the live server sends."""
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload))],
        structured_content=payload if structured else None,
    )


class TestResultParsing:
    """The mapping from a `tavily_search` result onto `SearchResult`."""

    def test_a_recorded_structured_result_maps_onto_search_results(self) -> None:
        """Proves title, url and snippet come from Tavily's title, url and content."""
        results = parse_search_tool_result(recorded_result(RECORDED_PAYLOAD), max_results=5)

        assert [result.url for result in results] == [
            "https://example.org/first-week.pdf",
            "https://example.org/settling-in",
        ]
        assert all(isinstance(result, SearchResult) for result in results)
        assert all(result.title for result in results)

    def test_snippets_are_cut_to_four_hundred_characters(self) -> None:
        """Proves a long page excerpt never exceeds the snippet limit."""
        results = parse_search_tool_result(recorded_result(RECORDED_PAYLOAD), max_results=5)

        assert SNIPPET_CHARACTER_LIMIT == 400
        assert all(len(result.snippet) <= SNIPPET_CHARACTER_LIMIT for result in results)

    def test_the_result_count_respects_max_results(self) -> None:
        """Proves the client never returns more results than were asked for."""
        results = parse_search_tool_result(recorded_result(RECORDED_PAYLOAD), max_results=1)

        assert len(results) == 1

    def test_a_text_only_result_is_parsed_from_its_json_block(self) -> None:
        """Proves the parser works when the server omits structured content."""
        results = parse_search_tool_result(
            recorded_result(RECORDED_PAYLOAD, structured=False), max_results=5
        )

        assert len(results) == 2

    def test_a_hit_without_a_url_is_dropped(self) -> None:
        """Proves the agent is never handed a source it could not cite (R4)."""
        payload = {"results": [{"title": "No link", "content": "text", "url": ""}]}

        assert parse_search_tool_result(recorded_result(payload), max_results=5) == []

    def test_a_tool_error_result_raises(self) -> None:
        """Proves an `is_error` result is a failure, not an empty success."""
        error_result = CallToolResult(
            content=[TextContent(type="text", text="Invalid API key")], is_error=True
        )

        with pytest.raises(TavilyMcpProtocolError):
            parse_search_tool_result(error_result, max_results=5)

    def test_an_error_inside_a_normal_result_raises(self) -> None:
        """Proves a rejected key, which Tavily reports in-payload with is_error false, fails."""
        rejected_key_payload = {
            "error": "Search failed",
            "status": 401,
            "detail": {"error": "Unauthorized: missing or invalid API key."},
        }

        with pytest.raises(TavilyMcpProtocolError, match="401"):
            parse_search_tool_result(recorded_result(rejected_key_payload), max_results=5)

    def test_a_payload_without_results_raises(self) -> None:
        """Proves a changed response contract fails loudly rather than as 'no hits'."""
        with pytest.raises(TavilyMcpProtocolError):
            parse_search_tool_result(recorded_result({"answer": "x"}), max_results=5)


class TestToolDiscovery:
    """Finding the search tool and its arguments in the live listing."""

    def test_the_search_tool_is_found_among_the_advertised_tools(self) -> None:
        """Proves discovery picks `tavily_search` out of the server's five tools."""
        tools = [make_tool(name) for name in ("tavily_extract", "tavily_search", "tavily_map")]

        assert select_search_tool(tools).name == "tavily_search"

    def test_the_hyphenated_tool_name_is_also_accepted(self) -> None:
        """Proves the older `tavily-search` spelling of the tool still matches."""
        assert select_search_tool([make_tool("tavily-search")]).name == "tavily-search"

    def test_a_server_without_a_search_tool_is_rejected(self) -> None:
        """Proves a listing with no search tool raises instead of calling a guess."""
        with pytest.raises(TavilyMcpProtocolError):
            select_search_tool([make_tool("tavily_extract")])

    def test_arguments_follow_the_advertised_schema(self) -> None:
        """Proves query and max_results are sent when the schema declares both."""
        arguments = build_search_arguments(make_tool("tavily_search"), "dog food", 3)

        assert arguments == {"query": "dog food", "max_results": 3}

    def test_an_undeclared_max_results_is_not_sent(self) -> None:
        """Proves the client never sends an argument the tool does not accept."""
        schema = {"type": "object", "properties": {"query": {"type": "string"}}}

        arguments = build_search_arguments(make_tool("tavily_search", schema), "dog food", 3)

        assert arguments == {"query": "dog food"}

    def test_a_schema_without_a_query_argument_is_rejected(self) -> None:
        """Proves a renamed query parameter fails loudly instead of searching for nothing."""
        schema = {"type": "object", "properties": {"q": {"type": "string"}}}

        with pytest.raises(TavilyMcpProtocolError):
            build_search_arguments(make_tool("tavily_search", schema), "dog food", 3)


class TestSecretHandling:
    """The API key must never reach a message, a log line or a trace."""

    def test_the_raw_and_url_encoded_key_are_scrubbed(self) -> None:
        """Proves both spellings of the key are replaced, since a URL encodes it."""
        from urllib.parse import quote

        text = f"GET /mcp/?tavilyApiKey={quote(FAKE_API_KEY, safe='')} key={FAKE_API_KEY}"

        scrubbed = scrub_secret(text, FAKE_API_KEY)

        assert FAKE_API_KEY not in scrubbed
        assert quote(FAKE_API_KEY, safe="") not in scrubbed

    def test_an_exception_group_is_described_by_its_leaf_without_the_key(self) -> None:
        """Proves the transport's task-group wrapper is unwrapped and scrubbed."""
        leaf = ConnectionError(f"refused for {FAKE_API_KEY}")
        group = ExceptionGroup("unhandled errors in a TaskGroup", [leaf])

        description = describe_failure_without_secret(group, FAKE_API_KEY)

        assert description.startswith("ConnectionError")
        assert FAKE_API_KEY not in description


class TestProvider:
    """`TavilyMcpSearchProvider` keeps the WebSearchProvider contract."""

    def test_a_successful_remote_search_is_returned_unchanged(self) -> None:
        """Proves the provider passes key, query and limit to the MCP call."""
        calls: list[tuple[str, str, int]] = []
        expected = [SearchResult("Title", "https://example.org", "snippet")]

        def fake_remote_search(api_key: str, query: str, max_results: int) -> list[SearchResult]:
            calls.append((api_key, query, max_results))
            return expected

        provider = TavilyMcpSearchProvider(FAKE_API_KEY, remote_search=fake_remote_search)

        assert provider.search("dog food", max_results=2) == expected
        assert calls == [(FAKE_API_KEY, "dog food", 2)]
        assert provider.last_failure is None

    def test_any_failure_degrades_to_no_results(self, caplog: pytest.LogCaptureFixture) -> None:
        """Proves a failed search returns [] and never leaks the key into logs."""

        def failing_remote_search(api_key: str, query: str, max_results: int) -> list[SearchResult]:
            raise RuntimeError(f"401 for https://mcp.tavily.com/mcp/?tavilyApiKey={api_key}")

        provider = TavilyMcpSearchProvider(FAKE_API_KEY, remote_search=failing_remote_search)

        with caplog.at_level(logging.DEBUG):
            results = provider.search("dog food")

        assert results == []
        assert provider.last_failure is not None
        assert FAKE_API_KEY not in provider.last_failure
        assert FAKE_API_KEY not in caplog.text
        assert "Tavily MCP search failed" in caplog.text

    def test_build_search_provider_picks_mcp_with_a_key_and_stub_without(self) -> None:
        """Proves configuration alone decides between live MCP search and the stub."""
        assert isinstance(build_search_provider(FAKE_API_KEY), TavilyMcpSearchProvider)
        assert isinstance(build_search_provider(""), StubSearchProvider)


class TestStreamableHttpWire:
    """The real MCP client path, answered by an in-process mock transport."""

    def test_the_key_travels_as_a_header_never_in_the_url(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Proves the request carries a bearer header, no key in the URL, and fails safely.

        The mock server answers 401 to the MCP `initialize` POST, which is
        what Tavily does for a bad key. The provider must turn that into []
        with nothing secret in its failure message or in any log record,
        including the MCP SDK's own debug logging.
        """
        seen_requests: list[httpx2.Request] = []

        def reject(request: httpx2.Request) -> httpx2.Response:
            seen_requests.append(request)
            return httpx2.Response(401, json={"error": "unauthorized"})

        real_client = httpx2.AsyncClient

        def client_with_mock_transport(
            *, headers: dict[str, str], timeout: httpx2.Timeout
        ) -> httpx2.AsyncClient:
            return real_client(
                transport=httpx2.MockTransport(reject), headers=headers, timeout=timeout
            )

        monkeypatch.setattr(tavily_mcp.httpx2, "AsyncClient", client_with_mock_transport)
        provider = TavilyMcpSearchProvider(FAKE_API_KEY, remote_search=search_tavily_over_mcp)

        with caplog.at_level(logging.DEBUG):
            results = provider.search("dog food")

        assert results == []
        assert seen_requests, "the MCP client never reached the transport"
        first_request = seen_requests[0]
        assert first_request.headers["Authorization"] == f"Bearer {FAKE_API_KEY}"
        assert FAKE_API_KEY not in str(first_request.url)
        assert str(first_request.url) == tavily_mcp.TAVILY_MCP_URL
        assert provider.last_failure is not None
        assert FAKE_API_KEY not in provider.last_failure
        assert FAKE_API_KEY not in caplog.text
