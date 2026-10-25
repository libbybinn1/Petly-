"""The shape of one web-search result the agent may cite (spec section 13).

Kept in its own module so the search policy (`web_search`) and the MCP wire
client (`tavily_mcp`) can both use it without importing each other.
"""

from __future__ import annotations

from dataclasses import dataclass

SNIPPET_CHARACTER_LIMIT = 400


@dataclass(frozen=True)
class SearchResult:
    """One external result the agent may cite."""

    title: str
    url: str
    snippet: str
