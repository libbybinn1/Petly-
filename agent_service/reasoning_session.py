"""The working set of one analysis task.

Course blueprint section 6.2 requires the agent to observe tool results and
update its plan. That means one task accumulates state: what it has looked
at, what it found, what it could not find, and whether it has spent its one
web search (spec section 13).

Kept in its own module because both the loop that gathers evidence and the
explanation layer that grounds prose against it need to read the same state,
and neither should have to import the other to do so.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent_service.rag.knowledge_base import RetrievedChunk
from agent_service.tools.web_search import SearchResult


@dataclass
class ReasoningStep:
    """One observable step of the loop, kept for auditability."""

    step_number: int
    action: str
    detail: str


@dataclass
class ReasoningSession:
    """Everything one analysis task has gathered so far.

    Gathered in one object rather than threaded through a dozen parameters,
    because every step of the loop reads or adds to the same four things: the
    trace, the evidence, the gaps found, and whether the web has been used -
    which the spec section 13 gate needs to know before it can answer
    honestly.
    """

    trace: list[ReasoningStep] = field(default_factory=list)
    retrieved: list[RetrievedChunk] = field(default_factory=list)
    search_results: list[SearchResult] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    has_searched_web: bool = False

    def record(self, action: str, detail: str) -> None:
        """Append one step to the trace, numbering it in order.

        Args:
            action: What the agent did, named as the tool or the decision.
            detail: The observation, short enough to read in the interface.
        """
        self.trace.append(ReasoningStep(len(self.trace) + 1, action, detail))

    def note_gap(self, description: str) -> None:
        """Record something the assessment needed but does not have.

        Duplicates are ignored, so a tool that fails twice does not fill
        `missing_information` with one repeated sentence.

        Args:
            description: One sentence naming what is missing.
        """
        if description not in self.gaps:
            self.gaps.append(description)

    def add_passages(self, passages: list[RetrievedChunk]) -> list[RetrievedChunk]:
        """Add newly retrieved passages, ignoring any already held.

        Args:
            passages: What a retrieval returned.

        Returns:
            Only the passages that were new to this task.
        """
        known_ids = {held.chunk.chunk_id for held in self.retrieved}
        fresh = [passage for passage in passages if passage.chunk.chunk_id not in known_ids]
        self.retrieved.extend(fresh)
        return fresh

    @property
    def known_references(self) -> list[str]:
        """Every source reference this task actually retrieved.

        The grounding check compares the model's citations against exactly
        this list, so nothing the agent did not read can be presented as
        evidence (rule R4).
        """
        return [passage.chunk.citation for passage in self.retrieved] + [
            result.url for result in self.search_results
        ]
