"""The agent's reason-act-observe loop.

Course blueprint section 6 requires a real agent rather than a single model
call: it plans, selects tools, observes results and revises its plan. This
module implements that cycle for one analysis task.

How one task runs:

1. **Deterministic prerequisites.** Both records are fetched through the MCP
   tools, the score is calculated, and one curated-knowledge retrieval runs
   with the web gate applied to it. These become the loop's first
   observations. They are not negotiable: an explanation must never be
   written over records the agent could not read, and the score has to exist
   whatever the model does.
2. **The bounded reason-act loop.** The model receives the facts, the score,
   every observation so far and a tool manifest, and answers with *either* a
   tool call or its final explanation. A tool call is dispatched, its result
   is appended as an observation, and the model is asked again - at most
   `max_reasoning_steps` times. The model chooses; nothing here chooses for
   it (blueprint section 6.2).
3. **Grounding.** The answer is checked against the evidence actually
   retrieved before any of it is stored (see `agent_service.explanation`).

The division of labour is the important part, and it is not negotiable
(spec section 8, docs/AGENT.md section 1):

    deterministic Python  ->  eligibility, every criterion score, the total
    the language model    ->  which evidence to gather, and the words
                              explaining that total

So a model outage degrades PetMatch to *scores without prose*, not to no
service at all.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.domain.enums import MatchDirection, Species
from app.domain.facts import (
    adopter_facts_from,
    animal_facts_from,
    parse_optional_whole_number,
)
from app.domain.matching import AdopterFacts, AnimalFacts, MatchScore, calculate_match_score
from app.infrastructure.clock import aware_utc_now

from agent_service.explanation import (
    DETERMINISTIC_FALLBACK_MODEL,
    build_evidence,
    build_explanation_prompt,
    deterministic_explanation,
    ground_explanation,
    merge_missing_information,
    shorten,
)
from agent_service.llm_client import (
    ChatMessage,
    LanguageModel,
    LanguageModelUnavailableError,
    MalformedModelOutputError,
    ToolCallRequest,
    ToolSchema,
    next_model_turn,
)
from agent_service.rag.knowledge_base import KnowledgeRetriever
from agent_service.reasoning_session import ReasoningSession, ReasoningStep
from agent_service.tools.mcp_tools import ProfileLookup, ToolAdvertiser, ToolDefinition
from agent_service.tools.web_search import WebSearchProvider, decide_whether_to_search

PROMPT_DIRECTORY = Path(__file__).resolve().parent / "prompts"

# Observations are fed back to a 3-billion-parameter model with a finite
# context window, so each one is trimmed rather than pasted whole.
MAX_OBSERVATION_CHARACTERS = 1200
MAX_RECORD_PREVIEW_CHARACTERS = 800
MAX_TRACE_DETAIL_CHARACTERS = 200
MAX_QUERY_IN_TRACE_CHARACTERS = 120

WEB_RESULT_COUNT = 3

TOOL_RAG_SEARCH = "rag_search"
TOOL_WEB_SEARCH = "web_search"
MCP_ADOPTER_TOOL = "get_adopter_profile"
MCP_ANIMAL_TOOL = "get_animal_profile"

# The argument names those two tools declare in their manifests, which is
# what a well-behaved call uses.
MCP_ADOPTER_ARGUMENT = "adopter_profile_id"
MCP_ANIMAL_ARGUMENT = "animal_id"

# The two tools the agent owns itself. The MCP tools are deliberately absent:
# their descriptions are read from the server at runtime, because docs/MCP.md
# promises that the server's own docstring is what the model sees.
LOCAL_TOOL_DEFINITIONS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name=TOOL_RAG_SEARCH,
        description=(
            "Search PetMatch's own curated adoption knowledge base: the "
            "organisation's care guides on space and housing, activity and "
            "exercise, children in the household, multi-pet homes, senior "
            "and special-needs animals, and the adoption policy. Use this "
            "FIRST for any question about animal care or suitability. "
            "Retrieval is semantic, so ask a plain question in your own "
            "words rather than keywords. Returns passages, each with a "
            "citation reference you must quote exactly if you rely on it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The question to look up, in plain language.",
                }
            },
            "required": ["query"],
        },
    ),
    ToolDefinition(
        name=TOOL_WEB_SEARCH,
        description=(
            "Search the public web. Policy-gated and a last resort: it runs "
            "only for what the curated knowledge base cannot answer - which "
            "is usually a current or external fact such as a regulation, a "
            "disease outbreak, a recall or a price. If the guides already "
            "answered the question, this is refused however current the "
            "wording. Never use it for PetMatch's own adopter or animal "
            "records - those come from the profile tools. At most one web "
            "search per task. If the policy refuses your query you are told "
            "which rule refused it; re-plan rather than repeating the query."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to look up on the web, in plain language.",
                }
            },
            "required": ["query"],
        },
    ),
)


@dataclass
class AnalysisOutcome:
    """Everything one completed analysis produced."""

    score: MatchScore
    reasons: list[str]
    concerns: list[str]
    missing_information: list[str]
    evidence_sources: list[dict[str, object]]
    used_web_search: bool
    model_name: str
    reasoning_trace: list[ReasoningStep] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    generated_at: datetime = field(default_factory=aware_utc_now)

    @property
    def explanation_is_generated(self) -> bool:
        """Whether a model produced the prose, or it fell back to criteria text."""
        return self.model_name != DETERMINISTIC_FALLBACK_MODEL


class MissingRecordError(LookupError):
    """Raised when a record the task depends on does not exist."""


def load_prompt(file_name: str) -> str:
    """Read a versioned prompt file.

    Prompts live in version-controlled files rather than string literals, per
    spec section 16, so a change to the agent's instructions shows up in a
    diff like any other change.

    Args:
        file_name: The file to read from `agent_service/prompts/`.

    Returns:
        The prompt text.
    """
    return (PROMPT_DIRECTORY / file_name).read_text(encoding="utf-8")


class MatchAnalysisAgent:
    """Runs one matching analysis end to end."""

    def __init__(
        self,
        language_model: LanguageModel,
        knowledge_base: KnowledgeRetriever,
        mcp_client: ProfileLookup,
        search_provider: WebSearchProvider,
        max_reasoning_steps: int = 8,
    ) -> None:
        """Wire the agent to its tools."""
        self._language_model = language_model
        self._knowledge_base = knowledge_base
        self._mcp_client = mcp_client
        self._search_provider = search_provider
        self._max_reasoning_steps = max_reasoning_steps

    def analyse(
        self,
        adopter_profile_id: str,
        animal_id: str,
        direction: MatchDirection,
    ) -> AnalysisOutcome:
        """Assess one adopter against one animal.

        Args:
            adopter_profile_id: Whose profile to assess.
            animal_id: Which animal to assess them against.
            direction: Which weighting to apply.

        Returns:
            The score plus its grounded explanation, and the reasoning trace
            that produced it.

        Raises:
            MissingRecordError: If either record cannot be retrieved.
        """
        session = ReasoningSession()
        adopter_payload, animal_payload = self._fetch_records(
            adopter_profile_id, animal_id, session
        )

        adopter = build_adopter_facts(adopter_payload)
        animal = build_animal_facts(animal_payload)

        # Deterministic scoring. No model involved; this always succeeds.
        score = calculate_match_score(adopter, animal, direction)
        session.record(
            "calculate_score", f"score={score.score} disqualified={score.is_disqualified}"
        )

        _note_payload_gaps(adopter_payload, animal_payload, session)
        self._gather_first_evidence(adopter, animal, _breed_of(animal_payload), session)

        explanation = self._explain(adopter_payload, animal_payload, score, session)
        return _build_outcome(score, explanation, session)

    # ------------------------------------------------------------------
    # The deterministic prerequisites
    # ------------------------------------------------------------------

    def _fetch_records(
        self, adopter_profile_id: str, animal_id: str, session: ReasoningSession
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Fetch both records through the MCP tools before anything else.

        The agent never reads the application's database directly (rule R2),
        and it never scores a record it could not read: a confident number
        derived from nothing is exactly what spec section 6.4 forbids.

        Args:
            adopter_profile_id: Whose profile to fetch.
            animal_id: Which animal to fetch.
            session: The task state, which receives both observations.

        Returns:
            The adopter payload and the animal payload.

        Raises:
            MissingRecordError: If either record was not found.
        """
        adopter_payload = self._mcp_client.get_adopter_profile(adopter_profile_id)
        session.record(MCP_ADOPTER_TOOL, _describe(adopter_payload))

        animal_payload = self._mcp_client.get_animal_profile(animal_id)
        session.record(MCP_ANIMAL_TOOL, _describe(animal_payload))

        if not adopter_payload.get("found") or not animal_payload.get("found"):
            raise MissingRecordError(
                f"adopter found={adopter_payload.get('found')}, "
                f"animal found={animal_payload.get('found')}"
            )
        return adopter_payload, animal_payload

    def _gather_first_evidence(
        self,
        adopter: AdopterFacts,
        animal: AnimalFacts,
        breed: str | None,
        session: ReasoningSession,
    ) -> None:
        """Retrieve curated guidance for this pairing, then apply the web gate.

        Run before the model is asked anything, for two reasons: RAG-first is
        the policy (spec section 13), and a model that answers immediately
        should still be answering over evidence rather than over a bare
        record.

        Two retrievals: one about the pairing's situation, one about the
        animal itself. The knowledge base counts as having answered only
        when a retrieved passage names this animal - its breed, or its
        species when no breed is recorded. Generic apartment or exercise
        guidance always matches a situational question, so "anything came
        back" would keep the web gate shut for every pairing, including a
        Saluki the curated guides never mention (spec section 13).

        Args:
            adopter: The adopter's facts, used to phrase the question.
            animal: The animal's facts, used to phrase the question.
            breed: The animal's breed as recorded, if any.
            session: The task state, which receives the evidence.
        """
        self._search_knowledge(_knowledge_question_for(adopter, animal, breed), session)

        animal_question = _animal_care_question_for(animal, breed)
        self._search_knowledge(animal_question, session)

        subject = breed or _species_words(animal)
        is_covered = knowledge_covers_animal(session, animal, breed)
        session.record(
            "knowledge_coverage",
            f'curated guides {"cover" if is_covered else "do not mention"} "{subject}"',
        )
        self._search_web(animal_question, session, knowledge_covers_question=is_covered)

    # ------------------------------------------------------------------
    # The tools, each returning the observation the model will read
    # ------------------------------------------------------------------

    def _search_knowledge(self, query: str, session: ReasoningSession) -> str:
        """Search the curated knowledge base and observe the result.

        Args:
            query: The question to retrieve against.
            session: The task state, which receives the passages.

        Returns:
            The observation to feed back to the model.
        """
        if not query.strip():
            session.record(TOOL_RAG_SEARCH, "refused: empty query")
            return "rag_search needs a non-empty query."

        try:
            passages = self._knowledge_base.search_relevant(query)
        except Exception as error:  # - a tool failure must not end the loop
            return _observe_tool_failure(TOOL_RAG_SEARCH, error, session)

        fresh = session.add_passages(passages)
        session.record(
            TOOL_RAG_SEARCH,
            f'query="{shorten(query, MAX_QUERY_IN_TRACE_CHARACTERS)}" -> '
            f"{len(passages)} relevant passage(s), {len(fresh)} new",
        )
        if not passages:
            session.note_gap(
                "The curated knowledge base held nothing relevant to this pairing."
            )
            return (
                "rag_search returned nothing relevant: the curated knowledge base "
                "does not cover this question."
            )
        return "rag_search returned:\n" + "\n".join(
            f"[{passage.chunk.citation}] {shorten(passage.chunk.text)}" for passage in passages
        )

    def _search_web(
        self,
        query: str,
        session: ReasoningSession,
        *,
        knowledge_covers_question: bool | None = None,
    ) -> str:
        """Apply the spec section 13 gate, and search only if it opens.

        The gate's inputs are live rather than assumed: whether the knowledge
        base has already answered, and whether this task has already spent
        its one web search. A refusal goes back to the model with its reason,
        so it can re-plan instead of repeating itself.

        Args:
            query: What is to be looked up.
            session: The task state, which receives any results.
            knowledge_covers_question: Whether the curated guides answered
                this specific question. Left unset for a search the model
                asks for, where any passage already held counts.

        Returns:
            The observation to feed back to the model.
        """
        decision = decide_whether_to_search(
            query,
            relevant_knowledge_found=(
                bool(session.retrieved)
                if knowledge_covers_question is None
                else knowledge_covers_question
            ),
            already_searched_this_task=session.has_searched_web,
        )
        session.record(
            "web_search_gate",
            f'query="{shorten(query, MAX_QUERY_IN_TRACE_CHARACTERS)}" -> {decision.value}',
        )

        if not decision.is_allowed:
            if not session.retrieved:
                session.note_gap(
                    "No curated guidance covered this pairing and the web-search "
                    f"policy declined to look further ({decision.value})."
                )
            return (
                f"web_search was refused by policy: {decision.value}. Do not repeat "
                f"this query; use the evidence you already have."
            )

        session.has_searched_web = True
        try:
            results = self._search_provider.search(query, max_results=WEB_RESULT_COUNT)
        except Exception as error:  # - a provider failure must not end the loop
            return _observe_tool_failure(TOOL_WEB_SEARCH, error, session)

        session.search_results.extend(results)
        session.record(TOOL_WEB_SEARCH, f"{len(results)} result(s) after {decision.value}")
        if not results:
            session.note_gap("A web search was permitted but returned no usable result.")
            return "web_search returned no results."
        return "web_search returned:\n" + "\n".join(
            f"[{result.url}] {result.title}: {shorten(result.snippet)}" for result in results
        )

    def _fetch_record_again(
        self, tool_name: str, arguments: dict[str, Any], session: ReasoningSession
    ) -> str:
        """Re-fetch a record because the model asked for it.

        Both MCP tools are read-only, so honouring the request costs one
        subprocess round trip and nothing else. Offering a tool in the
        manifest and then refusing to run it would make the manifest a lie.

        Args:
            tool_name: Which MCP tool the model named.
            arguments: The arguments it chose.
            session: The task state, which receives the observation.

        Returns:
            The observation to feed back to the model.
        """
        identifier = _identifier_argument(arguments)
        try:
            payload = self._call_profile_tool(tool_name, identifier)
        except Exception as error:  # - a tool failure must not end the loop
            return _observe_tool_failure(tool_name, error, session)

        session.record(tool_name, f"{identifier or '(no identifier)'} -> {_describe(payload)}")
        return f"{tool_name} returned: " + shorten(
            json.dumps(payload, default=str), MAX_RECORD_PREVIEW_CHARACTERS
        )

    def _call_profile_tool(self, tool_name: str, identifier: str) -> dict[str, Any]:
        """Call one of the two MCP record tools by name."""
        if tool_name == MCP_ADOPTER_TOOL:
            return self._mcp_client.get_adopter_profile(identifier)
        return self._mcp_client.get_animal_profile(identifier)

    # ------------------------------------------------------------------
    # The loop itself
    # ------------------------------------------------------------------

    def _explain(
        self,
        adopter_payload: dict[str, Any],
        animal_payload: dict[str, Any],
        score: MatchScore,
        session: ReasoningSession,
    ) -> dict[str, Any]:
        """Run the reason-act loop and return the grounded explanation.

        Args:
            adopter_payload: The adopter record, as the tool returned it.
            animal_payload: The animal record, as the tool returned it.
            score: The calculated score, which the model may not change.
            session: The task state.

        Returns:
            The explanation fields, with `model_name` naming whoever wrote
            them - the model, or the deterministic fallback.
        """
        manifest = self._announce_manifest(session)
        messages: list[ChatMessage] = [
            {"role": "system", "content": load_prompt("explanation_system.md")},
            {
                "role": "user",
                "content": build_explanation_prompt(
                    adopter_payload, animal_payload, score, session
                ),
            },
        ]

        answer = self._take_turns(messages, manifest, session)
        if answer is None:
            return deterministic_explanation(score)
        return ground_explanation(answer, score, session, self._language_model.model_name)

    def _announce_manifest(self, session: ReasoningSession) -> list[ToolSchema]:
        """Build this task's tool manifest and record what it holds.

        Args:
            session: The task state, which receives the observation.

        Returns:
            The manifest in the form the model's API expects.
        """
        definitions, problem = build_tool_manifest(self._mcp_client)
        detail = ", ".join(definition.name for definition in definitions) or "(none)"
        if problem is not None:
            detail = f"{detail} (MCP manifest unavailable: {problem})"
            session.note_gap(
                "The MCP tool server could not be queried, so the agent could not "
                "offer its record tools to the model."
            )
        session.record("tool_manifest", detail)
        return [_as_model_tool(definition) for definition in definitions]

    def _take_turns(
        self,
        messages: list[ChatMessage],
        manifest: list[ToolSchema],
        session: ReasoningSession,
    ) -> dict[str, Any] | None:
        """Ask the model for its next move until it answers or the cap is hit.

        This is what the step cap actually bounds (blueprint section 6.2):
        each iteration is one model decision plus at most one tool call, so
        the loop terminates by construction.

        Args:
            messages: The conversation, extended in place as it runs.
            manifest: The tools the model may choose from.
            session: The task state.

        Returns:
            The model's final answer, or None when it never produced one -
            because the host was unreachable, the answer never parsed, or the
            step budget ran out.
        """
        step_count = 0
        while step_count < self._max_reasoning_steps:
            step_count += 1
            try:
                turn = next_model_turn(self._language_model, messages, manifest)
            except LanguageModelUnavailableError as error:
                session.record("model_unavailable", _trace_detail(error))
                return None
            except MalformedModelOutputError as error:
                session.record("malformed_answer", _trace_detail(error))
                return None

            if turn.is_final:
                answer = turn.final_content or {}
                session.record("final_answer", f"step {step_count}: keys={sorted(answer)}")
                return answer

            self._act_on(turn.tool_call, messages, session)

        session.record("step_cap_reached", f"stopped after {step_count} step(s)")
        session.note_gap(
            f"The agent reached its {self._max_reasoning_steps}-step reasoning "
            f"budget before writing an explanation."
        )
        return None

    def _act_on(
        self,
        tool_call: ToolCallRequest | None,
        messages: list[ChatMessage],
        session: ReasoningSession,
    ) -> None:
        """Dispatch the tool the model chose and append the observation.

        Args:
            tool_call: The call the model asked for.
            messages: The conversation, extended with the call and its result.
            session: The task state.
        """
        if tool_call is None:
            return

        observation = self._dispatch(tool_call, session)
        messages.append(
            {
                "role": "assistant",
                "content": f"Calling {tool_call.tool_name}({tool_call.argument_summary}).",
            }
        )
        messages.append(
            {"role": "tool", "content": shorten(observation, MAX_OBSERVATION_CHARACTERS)}
        )

    def _dispatch(self, tool_call: ToolCallRequest, session: ReasoningSession) -> str:
        """Route one model-chosen tool call to its implementation.

        An unknown name is reported back rather than raised: a small model
        occasionally invents a tool, and saying so is what lets it re-plan.

        Args:
            tool_call: The call the model asked for.
            session: The task state.

        Returns:
            The observation to feed back to the model.
        """
        if tool_call.tool_name == TOOL_RAG_SEARCH:
            return self._search_knowledge(_query_argument(tool_call), session)

        if tool_call.tool_name == TOOL_WEB_SEARCH:
            return self._search_web(_query_argument(tool_call), session)

        if tool_call.tool_name in (MCP_ADOPTER_TOOL, MCP_ANIMAL_TOOL):
            return self._fetch_record_again(tool_call.tool_name, tool_call.arguments, session)

        session.record("unknown_tool", tool_call.tool_name)
        return (
            f"There is no tool named {tool_call.tool_name}. Choose one of the tools "
            f"you were given, or answer with your final JSON object."
        )


# --------------------------------------------------------------------------
# The tool manifest
# --------------------------------------------------------------------------


def build_tool_manifest(mcp_client: ProfileLookup) -> tuple[list[ToolDefinition], str | None]:
    """Build the tools the model may choose from, asking the MCP server itself.

    Blueprint section 8 requires each local tool to carry a description that
    tells a model what it can do, and docs/MCP.md section 5 says that
    docstring is the only thing the model sees when deciding whether to call
    it. Both are only true if the manifest is read from the server at
    runtime, which is what this does.

    Args:
        mcp_client: The record-fetching client, queried for its advertised
            tools when it is able to advertise them.

    Returns:
        The manifest, and the reason its MCP half is missing when it is. A
        client that cannot be queried costs the model its record tools; it
        does not stop the analysis.
    """
    advertised, problem = _advertised_mcp_tools(mcp_client)
    return [*advertised, *LOCAL_TOOL_DEFINITIONS], problem


def _advertised_mcp_tools(mcp_client: ProfileLookup) -> tuple[list[ToolDefinition], str | None]:
    """Read the MCP server's advertised tools, degrading to none on failure."""
    if not isinstance(mcp_client, ToolAdvertiser):
        return [], "this client does not advertise tool definitions"

    try:
        advertised = mcp_client.list_tool_definitions()
    except Exception as error:  # - a dead tool server must not end the loop
        return [], f"{type(error).__name__}: {error}"

    return [item for item in advertised if isinstance(item, ToolDefinition)], None


def _as_model_tool(definition: ToolDefinition) -> ToolSchema:
    """Convert one definition into the shape a chat API expects."""
    return {
        "type": "function",
        "function": {
            "name": definition.name,
            "description": definition.description,
            "parameters": definition.input_schema,
        },
    }


def _identifier_argument(arguments: dict[str, Any]) -> str:
    """Read the record identifier an MCP tool call was made with.

    The declared names are tried first. Taking whatever happened to come out
    of the dictionary first was fine while a call carried one argument and
    wrong as soon as it carried two: a model that helpfully added
    `{"reason": "checking the yard", "animal_id": "..."}` had the reason
    fetched as an identifier, and the tool answered "no animal exists with
    identifier checking the yard".

    Falling back to the first string value is still better than refusing:
    a small model that names the argument something else plainly meant to
    fetch a record.

    Args:
        arguments: The arguments the model chose.

    Returns:
        The identifier, or an empty string when the call carried no text.
    """
    for name in (MCP_ADOPTER_ARGUMENT, MCP_ANIMAL_ARGUMENT):
        named = arguments.get(name)
        if isinstance(named, str) and named.strip():
            return named.strip()

    for value in arguments.values():
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _query_argument(tool_call: ToolCallRequest) -> str:
    """Read the query a search tool was called with.

    Small models sometimes name the argument something else - `q`, `question`
    or `text`. Taking the first string argument when `query` is absent is
    more useful than refusing a call that plainly meant to search.

    Args:
        tool_call: The call the model asked for.

    Returns:
        The query, or an empty string when the call carried no text.
    """
    named = tool_call.arguments.get("query")
    if isinstance(named, str) and named.strip():
        return named.strip()

    for value in tool_call.arguments.values():
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _trace_detail(error: Exception) -> str:
    """Render one exception short enough to sit in the reasoning trace."""
    return shorten(str(error), MAX_TRACE_DETAIL_CHARACTERS)


def _observe_tool_failure(tool_name: str, error: Exception, session: ReasoningSession) -> str:
    """Turn a tool exception into an observation and a recorded gap.

    Rule R4 and FR-10.10: a failing tool degrades the explanation, it does
    not end the loop. The model is told what failed so it can try something
    else, and the gap is surfaced to staff rather than hidden.

    Args:
        tool_name: The tool that raised.
        error: What it raised.
        session: The task state.

    Returns:
        The observation to feed back to the model.
    """
    reason = f"{type(error).__name__}: {error}"
    session.record(tool_name, f"failed: {shorten(reason, MAX_TRACE_DETAIL_CHARACTERS)}")
    session.note_gap(
        f"The {tool_name} tool failed ({shorten(reason, MAX_QUERY_IN_TRACE_CHARACTERS)}), "
        f"so any evidence it would have contributed is absent."
    )
    return f"tool {tool_name} failed: {reason}"


def _build_outcome(
    score: MatchScore, explanation: dict[str, Any], session: ReasoningSession
) -> AnalysisOutcome:
    """Assemble the stored outcome from the score, the prose and the evidence."""
    citations = [str(item) for item in explanation.get("citations", [])]
    return AnalysisOutcome(
        score=score,
        reasons=explanation["reasons"],
        concerns=explanation["concerns"],
        missing_information=merge_missing_information(
            session.gaps, explanation["missing_information"]
        ),
        evidence_sources=build_evidence(session, citations),
        used_web_search=bool(session.search_results),
        model_name=explanation["model_name"],
        reasoning_trace=session.trace,
        citations=citations,
    )


# --------------------------------------------------------------------------
# Converting tool payloads into domain value objects
# --------------------------------------------------------------------------


def build_adopter_facts(payload: dict[str, Any]) -> AdopterFacts:
    """Convert an MCP adopter payload into domain facts (spec section 8).

    A wrapper over `app.domain.facts`, which the web tier calls too. The
    agent used to own a second copy of this mapping, and it fell behind: it
    never read `preferred_age_range`, so the agent recomputed a score the web
    tier disagreed with and explained it with "the adopter expressed no age
    preference" about an adopter who had expressed one - a claim traceable to
    nothing, which rule R4 forbids.

    Args:
        payload: The record as the MCP tool returned it.

    Returns:
        The adopter's facts. Unknown enum values fall back to a safe default
        rather than raising: one unexpected string in one record must not
        stop the queue.
    """
    return adopter_facts_from(payload)


def build_animal_facts(payload: dict[str, Any]) -> AnimalFacts:
    """Convert an MCP animal payload into domain facts (spec section 8).

    Args:
        payload: The record as the MCP tool returned it.

    Returns:
        The animal's facts, mapped by the same rules the web tier uses.
    """
    return animal_facts_from(payload)


def _note_payload_gaps(
    adopter_payload: dict[str, Any], animal_payload: dict[str, Any], session: ReasoningSession
) -> None:
    """Record fields the criteria need that the records do not state.

    Deterministic, and therefore reliable in a way a model's own account of
    what it lacked is not. docs/AGENT.md section 10 promises this behaviour
    and nothing used to write it.

    Args:
        adopter_payload: The adopter record as the tool returned it.
        animal_payload: The animal record as the tool returned it.
        session: The task state, which receives the gaps.
    """
    children_present = bool(adopter_payload.get("household_has_children"))
    youngest_age = parse_optional_whole_number(adopter_payload.get("youngest_child_age"))
    if children_present and youngest_age is None:
        session.note_gap(
            "Children live in the household but the youngest child's age is not "
            "recorded, and the child-safety rule needs it."
        )

    if adopter_payload.get("daily_hours_available") is None:
        session.note_gap(
            "The profile does not state how many hours a day the adopter has "
            "available for an animal."
        )

    if not adopter_payload.get("is_complete"):
        session.note_gap(
            "The adopter's profile is incomplete, so some criteria were scored "
            "from defaults rather than from stated facts."
        )

    if animal_payload.get("has_special_needs") and not animal_payload.get(
        "special_needs_description"
    ):
        session.note_gap(
            "The animal is recorded as having special needs, but what they are "
            "is not described."
        )


def _knowledge_question_for(
    adopter: AdopterFacts, animal: AnimalFacts, breed: str | None = None
) -> str:
    """Compose the question put to the knowledge base.

    Built from the pairing's actual characteristics, so retrieval is about
    this specific match rather than a generic query. The breed leads when
    there is one: "a calm other" names no animal at all, and a Greek tortoise
    is what the guidance has to be about.

    Args:
        adopter: The adopter's facts.
        animal: The animal's facts.
        breed: The recorded breed, if any.

    Returns:
        One plain-language question.
    """
    parts = [
        f"A {animal.temperament.value.lower()} {_animal_noun(animal, breed)}",
        f"with {animal.activity_level.value.lower()} activity needs",
        f"joining a {adopter.home_type.value.lower()} home",
        f"with {adopter.daily_hours_available:g} hours available daily",
    ]
    if adopter.household_has_children:
        parts.append("where children live")
    if adopter.has_other_animals:
        parts.append("that already has other animals")
    if animal.has_special_needs:
        parts.append("and the animal has special care needs")
    return ", ".join(parts) + "."


def _animal_care_question_for(animal: AnimalFacts, breed: str | None) -> str:
    """The question about the animal itself, for the guides and then the web.

    Deliberately free of anything about the adopter: it may leave this
    machine as a web query, and it must never read as a request for the
    application's own records (spec section 13).

    Args:
        animal: The animal's facts.
        breed: The recorded breed, if any.

    Returns:
        One short query naming the animal and what an adopter needs to know.
    """
    return f"{_animal_noun(animal, breed)} care needs, temperament, exercise and housing"


def knowledge_covers_animal(
    session: ReasoningSession, animal: AnimalFacts, breed: str | None
) -> bool:
    """Whether any retrieved passage is about this particular animal.

    The breed is the subject when one is recorded, since breed-specific
    needs - a Saluki's sprinting, a Holland Lop's diet - are exactly what a
    general species guide does not say. Without a breed the species words
    stand in, and for an unnamed OTHER any relevant passage has to do.

    Args:
        session: The task state, holding every relevant passage retrieved.
        animal: The animal's facts.
        breed: The recorded breed, if any.

    Returns:
        True when the curated guides named the animal.
    """
    if not session.retrieved:
        return False
    subject = _normalised(breed or _species_words(animal))
    if not subject:
        return True
    # Whole words, so "pug" is not found inside "pugnacious"; a plural ending
    # on the last word allowed, because guides write "Border Collies".
    subject_pattern = re.compile(rf"\b{re.escape(subject)}(?:s|es)?\b")
    return any(
        subject_pattern.search(_normalised(f"{passage.chunk.heading} {passage.chunk.text}"))
        for passage in session.retrieved
    )


def _animal_noun(animal: AnimalFacts, breed: str | None) -> str:
    """"Saluki dog", "Greek Tortoise", or "cat" when no breed is known."""
    species = _species_words(animal)
    if not breed:
        return species or "animal"
    if animal.species is Species.OTHER or species in breed.lower():
        return breed
    return f"{breed} {species}"


def _species_words(animal: AnimalFacts) -> str:
    """The species in plain words; empty for OTHER, which names nothing."""
    if animal.species is Species.OTHER:
        return ""
    return animal.species.value.replace("_", " ").lower()


def _breed_of(animal_payload: dict[str, Any]) -> str | None:
    """The recorded breed from the MCP payload, or None when blank."""
    breed = str(animal_payload.get("breed") or "").strip()
    return breed or None


def _normalised(text: str) -> str:
    """Lower-case, with punctuation and runs of space reduced to one space.

    So "Hermann's Tortoise" in a record matches "hermann s tortoise" in a
    guide however the apostrophe was typed.
    """
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def _describe(payload: dict[str, Any]) -> str:
    """Summarise a tool payload for the reasoning trace."""
    if payload.get("found"):
        return payload.get("name") or payload.get("adopter_profile_id") or "record returned"
    return str(payload.get("reason", "not found"))
