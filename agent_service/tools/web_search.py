"""Web search, behind an explicit policy gate.

Spec section 13 requires web search to be deliberate rather than automatic:
RAG first for the curated knowledge the project maintains, the web only when
information is external, current, missing from the knowledge base or needs
verification, and never for the application's own adopter and animal records.

The policy is implemented as a separate, pure function rather than as
scattered `if` statements inside the agent loop. That makes the interesting
question testable: *did the agent correctly decide **not** to search?*
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

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
# knowledge base cannot answer well.
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


@dataclass(frozen=True)
class SearchResult:
    """One external result the agent may cite."""

    title: str
    url: str
    snippet: str


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

    if _needs_external_information(question):
        return SearchDecision.ALLOWED_EXTERNAL_TOPIC

    # Rule 1: RAG is the default source for stable domain knowledge.
    if relevant_knowledge_found:
        return SearchDecision.REFUSED_RAG_SUFFICIENT

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


class TavilySearchProvider:
    """Live web search through Tavily."""

    def __init__(self, api_key: str) -> None:
        """Bind the provider to an API key."""
        self._api_key = api_key

    def search(self, query: str, max_results: int = 3) -> list[SearchResult]:
        """Query Tavily, returning an empty list on any failure.

        A search failure is never fatal: the agent proceeds with the evidence
        it already has and records the gap under missing information.
        """
        from tavily import TavilyClient

        try:
            response = TavilyClient(api_key=self._api_key).search(
                query=query, max_results=max_results
            )
        except Exception:  # - any provider failure degrades to "no evidence"
            return []

        return [
            SearchResult(
                title=str(item.get("title", "")),
                url=str(item.get("url", "")),
                snippet=str(item.get("content", ""))[:400],
            )
            for item in response.get("results", [])
        ]


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
    """Choose a live or stub provider depending on configuration."""
    if api_key:
        return TavilySearchProvider(api_key)
    return StubSearchProvider()
