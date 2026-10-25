"""Web search, behind an explicit policy gate.

Spec section 13 requires web search to be deliberate rather than automatic:
RAG first for the curated knowledge the project maintains, the web only for
what the curated guides cannot answer - which in practice is information that
is external, current or simply absent - and never for the application's own
adopter and animal records.

The ordering of the rules below carries that meaning and is not incidental.
Curated sufficiency is decided before the question's topic: an external-
sounding question the knowledge base has already answered is answered, and
reaching for the web anyway would make "RAG first" a preference rather than
a rule (CLAUDE.md R4).

The policy is implemented as a separate, pure function rather than as
scattered `if` statements inside the agent loop. That makes the interesting
question testable: *did the agent correctly decide **not** to search?*
"""

from __future__ import annotations

import logging
import re
from enum import StrEnum
from typing import Protocol

from agent_service.tools.search_result import SearchResult
from agent_service.tools.tavily_mcp import (
    RemoteSearch,
    describe_failure_without_secret,
    search_tavily_over_mcp,
)

__all__ = [
    "SearchDecision",
    "SearchResult",
    "StubSearchProvider",
    "TavilyMcpSearchProvider",
    "WebSearchProvider",
    "build_search_provider",
    "decide_whether_to_search",
]

logger = logging.getLogger(__name__)

# Phrases that indicate the question is about the application's own records.
# Those come from MCP tools; searching the web for them would be both useless
# and a spec 13 violation.
OWN_RECORD_MARKERS = (
    "adopter profile",
    "this adopter",
    "our adopter",
    "application status",
    "animal record",
    "our animals",
    "in our database",
    "in the system",
)

# Questions about current or external facts that a curated, slowly-changing
# knowledge base is unlikely to cover. Consulted only once the knowledge base
# has come back with nothing: these are keyword matches, and a keyword is not
# evidence that the curated answer was inadequate.
EXTERNAL_INFORMATION_MARKERS = (
    "current",
    "recent",
    "latest",
    "this year",
    "2025",
    "2026",
    "price",
    "cost of",
    "regulation",
    "law",
    "legal requirement",
    "vaccination schedule",
    "outbreak",
    "recall",
)


class SearchDecision(StrEnum):
    """Why the gate allowed or refused a web search."""

    ALLOWED_KNOWLEDGE_GAP = "ALLOWED_KNOWLEDGE_GAP"
    ALLOWED_EXTERNAL_TOPIC = "ALLOWED_EXTERNAL_TOPIC"
    REFUSED_RAG_SUFFICIENT = "REFUSED_RAG_SUFFICIENT"
    REFUSED_OWN_RECORDS = "REFUSED_OWN_RECORDS"
    REFUSED_ALREADY_SEARCHED = "REFUSED_ALREADY_SEARCHED"
    REFUSED_EMPTY_QUESTION = "REFUSED_EMPTY_QUESTION"

    @property
    def is_allowed(self) -> bool:
        """Whether this decision permits a search."""
        return self.name.startswith("ALLOWED")


class WebSearchProvider(Protocol):
    """Anything that can answer a web query.

    A protocol so the agent depends on the capability rather than on Tavily,
    and so tests can substitute a stub with no network.
    """

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        """Return external results for a query."""
        ...


def decide_whether_to_search(
    question: str,
    *,
    relevant_knowledge_found: bool,
    already_searched_this_task: bool,
) -> SearchDecision:
    """Apply the spec section 13 web-search policy.

    Args:
        question: What the agent wants to find out.
        relevant_knowledge_found: Whether RAG returned anything above the
            relevance threshold for this question.
        already_searched_this_task: Whether a search has already run for the
            current job.

    Returns:
        The decision, carrying its reason so the agent can record why it did
        or did not reach for the web.
    """
    if not question.strip():
        return SearchDecision.REFUSED_EMPTY_QUESTION

    # Rule 3 of the policy: never search the web for our own records.
    # Checked first, because this refusal holds even when RAG found nothing.
    if _is_about_own_records(question):
        return SearchDecision.REFUSED_OWN_RECORDS

    if already_searched_this_task:
        return SearchDecision.REFUSED_ALREADY_SEARCHED

    # Rule 1, and it outranks the external-topic rule below. "RAG first, web
    # second" (CLAUDE.md R4) means the web answers what the curated guides
    # *cannot*, so a question the guides did answer never reaches it - however
    # current-sounding its wording. The external-topic markers are keyword
    # matches on a phrase the agent composed, and they used to be tested
    # first: one occurrence of "current" in a query the knowledge base had
    # already answered was enough to open the gate, which contradicted this
    # rule, the system prompt and the tool's own description.
    if relevant_knowledge_found:
        return SearchDecision.REFUSED_RAG_SUFFICIENT

    # Nothing curated covers this. An external or current topic is the clearer
    # half of that gap, and is reported separately so the trace says which
    # kind of gap opened the gate.
    if _needs_external_information(question):
        return SearchDecision.ALLOWED_EXTERNAL_TOPIC

    return SearchDecision.ALLOWED_KNOWLEDGE_GAP


def _is_about_own_records(question: str) -> bool:
    """Whether the question concerns data the application already owns."""
    lowered = question.lower()
    return any(marker in lowered for marker in OWN_RECORD_MARKERS)


def _needs_external_information(question: str) -> bool:
    """Whether the question asks for current or external facts."""
    lowered = question.lower()
    return any(
        re.search(rf"\b{re.escape(marker)}", lowered)
        for marker in EXTERNAL_INFORMATION_MARKERS
    )


class TavilyMcpSearchProvider:
    """Live web search consumed through Tavily's remote MCP server.

    Requirement 9 of the course brief: external services are consumed via
    the MCP protocol. The search runs as an MCP `tools/call` over Streamable
    HTTP against Tavily's hosted server (docs/MCP.md section 9), not through
    a vendor REST SDK. The wire details live in `tavily_mcp`; this class
    only owns the spec section 13 failure policy.
    """

    def __init__(
        self,
        api_key: str,
        remote_search: RemoteSearch | None = None,
    ) -> None:
        """Bind the provider to an API key.

        Args:
            api_key: The Tavily API key. Sent only as a bearer header.
            remote_search: The function that performs the MCP round trip.
                Defaults to the live Streamable HTTP client; tests pass a
                fake so they never touch the network.
        """
        self._api_key = api_key
        self._remote_search = remote_search or search_tavily_over_mcp
        self.last_failure: str | None = None

    def search(self, query: str, max_results: int = 3) -> list[SearchResult]:
        """Query Tavily over MCP, returning an empty list on any failure.

        A search failure is never fatal (spec section 13): the agent proceeds
        with the evidence it already has and records the gap under missing
        information. The failure is kept in `last_failure` and logged, with
        the API key scrubbed from both.

        Args:
            query: The search text.
            max_results: The most results to return.

        Returns:
            The parsed results, or `[]` when the search failed.
        """
        self.last_failure = None
        try:
            return self._remote_search(self._api_key, query, max_results)
        except Exception as error:  # - any provider failure degrades to "no evidence"
            self.last_failure = describe_failure_without_secret(error, self._api_key)
            logger.warning("Tavily MCP search failed: %s", self.last_failure)
            return []


class StubSearchProvider:
    """Offline stand-in used when no API key is configured, and in tests.

    Returning results rather than raising keeps the whole system runnable
    without credentials, which is what makes the test suite offline-capable.
    Canned results let a test exercise the path where search *did* return
    something, without touching the network.
    """

    def __init__(self, canned_results: list[SearchResult] | None = None) -> None:
        """Configure the results to return, and record what was asked."""
        self._canned_results = canned_results or []
        self.queries: list[str] = []

    def search(self, query: str, max_results: int = 3) -> list[SearchResult]:
        """Record the query and return the canned results, respecting the limit."""
        self.queries.append(query)
        return self._canned_results[:max_results]


def build_search_provider(api_key: str) -> WebSearchProvider:
    """Choose a live or stub provider depending on configuration (spec section 13).

    Args:
        api_key: The configured Tavily key; empty when none is set.

    Returns:
        The Tavily MCP provider when a key exists, else the offline stub.
    """
    if api_key:
        return TavilyMcpSearchProvider(api_key)
    return StubSearchProvider()
