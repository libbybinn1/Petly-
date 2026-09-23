"""Tests for the bounded reason-act loop (blueprint section 6, 6.2, 8).

Blueprint section 6 requires the agent to work "through Acting/Reasoning,
tool selection, receiving results, and updating its plan". These tests hold
it to that: the *model's* tool choice is what reaches the tool, the step cap
really terminates the loop, the spec section 13 gate refuses what it should,
and nothing the agent did not retrieve can be presented as evidence.

No live model and no network. Every model here is a `StubLanguageModel` with
a scripted list of turns, which is what makes "the model decided to call
rag_search" a deterministic assertion rather than a hope. Per rule R3 nothing
asserts on generated wording.
"""

from __future__ import annotations

from typing import Any

import pytest
from agent_service.explanation import DETERMINISTIC_FALLBACK_MODEL
from agent_service.llm_client import (
    ChatMessage,
    LanguageModelUnavailableError,
    ModelTurn,
    StubLanguageModel,
    ToolSchema,
)
from agent_service.loop import (
    MCP_ADOPTER_TOOL,
    MCP_ANIMAL_TOOL,
    TOOL_RAG_SEARCH,
    TOOL_WEB_SEARCH,
    MatchAnalysisAgent,
    build_adopter_facts,
    build_animal_facts,
    build_tool_manifest,
)
from agent_service.rag.knowledge_base import RetrievedChunk
from agent_service.tools.mcp_tools import ToolDefinition
from agent_service.tools.web_search import SearchDecision, SearchResult, StubSearchProvider
from app.domain.enums import MatchDirection
from app.domain.matching import calculate_match_score

from tests.agent.test_agent_loop import (
    ADOPTER_PAYLOAD,
    ANIMAL_PAYLOAD,
    FakeKnowledgeBase,
    FakeMcpClient,
    make_chunk,
)

pytestmark = pytest.mark.agent

# A question the gate must let through whatever RAG returned, because a
# static corpus cannot answer it (spec section 13, rule 1).
EXTERNAL_QUESTION = "What are the current vaccination regulations for dogs?"

# A question about the application's own data, which must never reach the web
# (spec section 13, rule 2).
OWN_RECORDS_QUESTION = "What is in this adopter profile in our database?"


class ManifestAwareMcpClient(FakeMcpClient):
    """An MCP double that also advertises its tools, as the real client does."""

    def list_tool_definitions(self) -> list[ToolDefinition]:
        """Advertise the two record tools with model-facing descriptions."""
        self.calls.append("list_tool_definitions()")
        return [
            ToolDefinition(
                name=MCP_ADOPTER_TOOL,
                description="Retrieve an adopter's profile for compatibility matching.",
                input_schema={
                    "type": "object",
                    "properties": {"adopter_profile_id": {"type": "string"}},
                },
            ),
            ToolDefinition(
                name=MCP_ANIMAL_TOOL,
                description="Retrieve an animal's profile for compatibility matching.",
                input_schema={
                    "type": "object",
                    "properties": {"animal_id": {"type": "string"}},
                },
            ),
        ]


class UnqueryableMcpClient(FakeMcpClient):
    """An MCP double whose tool server cannot be reached."""

    def list_tool_definitions(self) -> list[ToolDefinition]:
        """Fail the way a dead subprocess does."""
        raise OSError("the tool server could not be started")


class BrokenKnowledgeBase:
    """A retriever whose embedding backend is down."""

    def __init__(self) -> None:
        """Record the queries it was asked, before failing each one."""
        self.queries: list[str] = []

    def search_relevant(self, query: str, result_count: int = 4) -> list[RetrievedChunk]:
        """Raise as an unreachable embedding host would."""
        self.queries.append(query)
        raise RuntimeError("embedding host refused the connection")


class FailingModel:
    """A model that answers some turns and then becomes unreachable."""

    def __init__(self, turns_before_failure: int) -> None:
        """Configure how many turns succeed before the host goes away."""
        self._turns_before_failure = turns_before_failure
        self.turns_taken = 0

    @property
    def model_name(self) -> str:
        """Identifier for the model that went away."""
        return "went-away"

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        """Not used by these tests; present to satisfy the protocol."""
        raise LanguageModelUnavailableError("host down")

    def complete_with_tools(
        self, messages: list[ChatMessage], tools: list[ToolSchema]
    ) -> ModelTurn:
        """Call one tool, then fail as a stopped Ollama host does."""
        self.turns_taken += 1
        if self.turns_taken > self._turns_before_failure:
            raise LanguageModelUnavailableError("connection refused mid-loop")
        return ModelTurn.calling(TOOL_RAG_SEARCH, {"query": "apartment living for dogs"})


def build_agent(
    language_model: Any = None,  # noqa: ANN401 - any LanguageModel-shaped double
    knowledge_base: Any = None,  # noqa: ANN401
    mcp_client: Any = None,  # noqa: ANN401
    search_provider: Any = None,  # noqa: ANN401
    max_reasoning_steps: int = 8,
) -> MatchAnalysisAgent:
    """Assemble an agent from test doubles."""
    return MatchAnalysisAgent(
        language_model=language_model or StubLanguageModel(),
        knowledge_base=knowledge_base or FakeKnowledgeBase([make_chunk()]),
        mcp_client=mcp_client or ManifestAwareMcpClient(),
        search_provider=search_provider or StubSearchProvider(),
        max_reasoning_steps=max_reasoning_steps,
    )


def actions_in(trace: list[Any]) -> list[str]:
    """The action names of a reasoning trace, in order."""
    return [step.action for step in trace]


def run(agent: MatchAnalysisAgent) -> Any:  # noqa: ANN401 - returns an AnalysisOutcome
    """Analyse the standard test pairing."""
    return agent.analyse("adopter-1", "animal-1", MatchDirection.ANIMAL_TO_ADOPTER)


class TestToolSelectionIsTheModels:
    """Blueprint 6.2: the model decides whether and which tool to invoke."""

    def test_a_scripted_rag_call_reaches_the_knowledge_base_with_its_query(self) -> None:
        """Proves the tool the model named runs, with the arguments it chose.

        This is the difference between an agent and a pipeline: the query
        that reaches the retriever is the model's, not one the loop composed.
        """
        knowledge_base = FakeKnowledgeBase([make_chunk()])
        model = StubLanguageModel(
            scripted_turns=[
                ModelTurn.calling(TOOL_RAG_SEARCH, {"query": "crate training a collie"}),
                ModelTurn.answering({"reasons": ["A reason."], "citations": []}),
            ]
        )

        run(build_agent(language_model=model, knowledge_base=knowledge_base))

        assert "crate training a collie" in knowledge_base.queries

    def test_the_manifest_is_sent_to_the_model_on_every_turn(self) -> None:
        """Proves the model is actually told what it may call (blueprint 8).

        Without this the tool descriptions are documentation rather than a
        manifest: the audit found zero `tools=` payloads reaching the model.
        """
        model = StubLanguageModel(
            scripted_turns=[
                ModelTurn.calling(TOOL_RAG_SEARCH, {"query": "anything"}),
                ModelTurn.answering({"reasons": ["A reason."]}),
            ]
        )

        run(build_agent(language_model=model))

        assert len(model.tool_manifests_seen) == 2
        for manifest in model.tool_manifests_seen:
            offered = {entry["function"]["name"] for entry in manifest}
            assert offered == {
                MCP_ADOPTER_TOOL,
                MCP_ANIMAL_TOOL,
                TOOL_RAG_SEARCH,
                TOOL_WEB_SEARCH,
            }

    def test_a_model_chosen_record_lookup_runs_through_mcp(self) -> None:
        """Proves the record tools in the manifest are really callable.

        Advertising a tool and then refusing to run it would make the
        manifest a lie, and the model would keep asking for it.
        """
        mcp_client = ManifestAwareMcpClient()
        model = StubLanguageModel(
            scripted_turns=[
                ModelTurn.calling(MCP_ANIMAL_TOOL, {"animal_id": "animal-1"}),
                ModelTurn.answering({"reasons": ["A reason."]}),
            ]
        )

        outcome = run(build_agent(language_model=model, mcp_client=mcp_client))

        assert len([call for call in mcp_client.calls if "get_animal_profile" in call]) == 2
        assert MCP_ANIMAL_TOOL in actions_in(outcome.reasoning_trace)

    def test_an_invented_tool_name_is_reported_back_rather_than_raised(self) -> None:
        """Proves a hallucinated tool leaves the loop able to re-plan.

        A negative test: small models invent tool names, and an exception
        here would fail the whole job over one bad turn.
        """
        model = StubLanguageModel(
            scripted_turns=[
                ModelTurn.calling("approve_the_adoption", {}),
                ModelTurn.answering({"reasons": ["A reason."]}),
            ]
        )

        outcome = run(build_agent(language_model=model))

        assert "unknown_tool" in actions_in(outcome.reasoning_trace)
        assert 0 <= outcome.score.score <= 100


class TestTheLoopTerminates:
    """The cap has to bound something, and the trace has to show what ran."""

    def test_the_loop_stops_when_the_model_answers_and_records_every_step(self) -> None:
        """Proves one answered turn ends the loop, and the trace is complete.

        The trace is the audit artefact for blueprint 6.2, so it must carry
        the prerequisites, the manifest and the final answer in order.
        """
        model = StubLanguageModel(scripted_turns=[ModelTurn.answering({"reasons": ["A."]})])

        outcome = run(build_agent(language_model=model))

        assert model.turns_taken == 1
        assert actions_in(outcome.reasoning_trace)[:3] == [
            MCP_ADOPTER_TOOL,
            MCP_ANIMAL_TOOL,
            "calculate_score",
        ]
        assert actions_in(outcome.reasoning_trace)[-1] == "final_answer"
        assert [step.step_number for step in outcome.reasoning_trace] == list(
            range(1, len(outcome.reasoning_trace) + 1)
        )

    def test_the_step_cap_terminates_a_model_that_never_answers(self) -> None:
        """Proves `max_reasoning_steps` is a real bound, not a list slice.

        A negative test for the loop itself: a model that only ever calls
        tools must still produce a storable analysis.
        """
        knowledge_base = FakeKnowledgeBase([make_chunk()])
        model = StubLanguageModel(
            scripted_turns=[
                ModelTurn.calling(TOOL_RAG_SEARCH, {"query": f"question {index}"})
                for index in range(8)
            ]
        )

        outcome = run(
            build_agent(
                language_model=model, knowledge_base=knowledge_base, max_reasoning_steps=2
            )
        )

        assert model.turns_taken == 2
        assert "step_cap_reached" in actions_in(outcome.reasoning_trace)
        assert outcome.model_name == DETERMINISTIC_FALLBACK_MODEL
        assert 0 <= outcome.score.score <= 100
        assert outcome.reasons or outcome.concerns
        assert any("budget" in item for item in outcome.missing_information)

    def test_an_unreachable_model_mid_loop_falls_back_to_the_score(self) -> None:
        """Proves FR-10.10: losing the model mid-loop costs prose, not the analysis."""
        model = FailingModel(turns_before_failure=1)

        outcome = run(build_agent(language_model=model, max_reasoning_steps=4))

        assert outcome.model_name == DETERMINISTIC_FALLBACK_MODEL
        assert outcome.explanation_is_generated is False
        assert "model_unavailable" in actions_in(outcome.reasoning_trace)
        assert 0 <= outcome.score.score <= 100


class TestTheScoreIsUntouchedByTheLoop:
    """Spec section 8: the number is arithmetic, whatever the loop does."""

    @pytest.mark.parametrize(
        "direction",
        [MatchDirection.ADOPTER_TO_ANIMAL, MatchDirection.ANIMAL_TO_ADOPTER],
    )
    def test_the_score_equals_the_deterministic_calculation(
        self, direction: MatchDirection
    ) -> None:
        """Proves the loop cannot move the score in either direction.

        Compared against `calculate_match_score` directly, so the assertion
        is about equality with the arithmetic rather than about a range.
        """
        expected = calculate_match_score(
            build_adopter_facts(ADOPTER_PAYLOAD), build_animal_facts(ANIMAL_PAYLOAD), direction
        )
        model = StubLanguageModel(
            scripted_turns=[
                ModelTurn.calling(TOOL_RAG_SEARCH, {"query": "anything"}),
                ModelTurn.answering({"score": 99, "reasons": ["A reason."]}),
            ]
        )

        outcome = build_agent(language_model=model).analyse("adopter-1", "animal-1", direction)

        assert outcome.score.score == expected.score
        assert outcome.score.is_disqualified == expected.is_disqualified


class TestTheWebSearchGateIsLoadBearing:
    """Spec section 13: the policy decides, with real inputs."""

    def test_a_model_request_about_our_own_records_never_reaches_the_web(self) -> None:
        """Proves rule 2 of spec 13 is enforced against the model, not just documented.

        The model asking is the realistic case the gate exists for: adopter
        records come from MCP tools, and searching the public web for them
        would be both useless and a policy breach.
        """
        search_provider = StubSearchProvider(
            [SearchResult(title="T", url="https://example.test/a", snippet="S")]
        )
        model = StubLanguageModel(
            scripted_turns=[
                ModelTurn.calling(TOOL_WEB_SEARCH, {"query": OWN_RECORDS_QUESTION}),
                ModelTurn.answering({"reasons": ["A reason."]}),
            ]
        )

        outcome = run(build_agent(language_model=model, search_provider=search_provider))

        assert search_provider.queries == []
        assert outcome.used_web_search is False
        assert any(
            SearchDecision.REFUSED_OWN_RECORDS.value in step.detail
            for step in outcome.reasoning_trace
        )

    def test_a_second_web_search_in_one_task_is_refused(self) -> None:
        """Proves `already_searched_this_task` is a live flag, not a hardcoded False.

        The audit found the gate was always told no search had run, so rule 3
        of spec 13 could never fire. One task gets one search.
        """
        search_provider = StubSearchProvider(
            [SearchResult(title="T", url="https://example.test/a", snippet="S")]
        )
        model = StubLanguageModel(
            scripted_turns=[
                ModelTurn.calling(TOOL_WEB_SEARCH, {"query": EXTERNAL_QUESTION}),
                ModelTurn.calling(TOOL_WEB_SEARCH, {"query": EXTERNAL_QUESTION}),
                ModelTurn.answering({"reasons": ["A reason."]}),
            ]
        )

        outcome = run(build_agent(language_model=model, search_provider=search_provider))

        assert len(search_provider.queries) == 1
        assert any(
            SearchDecision.REFUSED_ALREADY_SEARCHED.value in step.detail
            for step in outcome.reasoning_trace
        )
        assert outcome.used_web_search is True


class TestToolFailuresDegrade:
    """Rule R4 and FR-10.10: a failing tool is observed, not fatal."""

    def test_a_raising_retriever_becomes_missing_information(self) -> None:
        """Proves an embedding outage is reported to staff and the loop continues.

        The knowledge base raising used to fail the whole job, which
        contradicted the documented promise that a model outage costs only
        the prose.
        """
        knowledge_base = BrokenKnowledgeBase()
        model = StubLanguageModel(
            scripted_turns=[
                ModelTurn.calling(TOOL_RAG_SEARCH, {"query": "anything"}),
                ModelTurn.answering({"reasons": ["A reason."]}),
            ]
        )

        outcome = run(build_agent(language_model=model, knowledge_base=knowledge_base))

        assert any(TOOL_RAG_SEARCH in item for item in outcome.missing_information)
        assert 0 <= outcome.score.score <= 100
        assert outcome.evidence_sources == []

    def test_an_unqueryable_tool_server_still_leaves_the_local_tools(self) -> None:
        """Proves a dead MCP subprocess costs two tools, not the analysis."""
        definitions, problem = build_tool_manifest(UnqueryableMcpClient())

        assert problem is not None
        assert [definition.name for definition in definitions] == [
            TOOL_RAG_SEARCH,
            TOOL_WEB_SEARCH,
        ]

    def test_an_analysis_completes_when_the_manifest_cannot_be_built(self) -> None:
        """Proves the loop runs with a shortened manifest rather than failing."""
        outcome = run(build_agent(mcp_client=UnqueryableMcpClient()))

        assert 0 <= outcome.score.score <= 100
        assert any("MCP manifest unavailable" in step.detail for step in outcome.reasoning_trace)


class TestGroundingIsEnforced:
    """Rule R4: every claim traces to something the agent actually retrieved."""

    def test_an_unknown_citation_is_dropped_and_recorded(self) -> None:
        """Proves a fabricated source cannot reach the database or the screen.

        The prompt already forbade it; a prompt is not an enforcement
        mechanism, so the citation is checked against what was retrieved.
        """
        retrieved = make_chunk("space-and-housing.md")
        real_reference = retrieved.chunk.citation
        model = StubLanguageModel(
            scripted_turns=[
                ModelTurn.answering(
                    {
                        "reasons": [
                            f"The home suits this animal [{real_reference}].",
                            "Per [invented-guidance-2019.md] this pairing is ideal.",
                        ],
                        "concerns": [],
                        "missing_information": [],
                        "citations": [real_reference, "invented-guidance-2019.md"],
                    }
                )
            ]
        )

        outcome = run(
            build_agent(
                language_model=model, knowledge_base=FakeKnowledgeBase([retrieved])
            )
        )

        assert outcome.citations == [real_reference]
        assert not any("invented-guidance-2019.md" in reason for reason in outcome.reasons)
        assert "dropped_citations" in actions_in(outcome.reasoning_trace)

    def test_evidence_marks_only_the_sources_that_were_cited(self) -> None:
        """Proves the interface can tell what was read from what was used.

        Spec 6.4 requires sources to be recorded when they materially affect
        an explanation; recording all of them and flagging the cited ones
        keeps both facts.
        """
        cited_chunk = make_chunk("space-and-housing.md")
        model = StubLanguageModel(
            scripted_turns=[
                ModelTurn.answering(
                    {
                        "reasons": [f"The home suits it [{cited_chunk.chunk.citation}]."],
                        "citations": [cited_chunk.chunk.citation],
                    }
                )
            ]
        )
        search_provider = StubSearchProvider(
            [SearchResult(title="T", url="https://example.test/unused", snippet="S")]
        )

        outcome = run(
            build_agent(
                language_model=model,
                knowledge_base=FakeKnowledgeBase([cited_chunk]),
                search_provider=search_provider,
            )
        )

        cited = {
            str(source["reference"]) for source in outcome.evidence_sources if source["cited"]
        }
        assert cited == {cited_chunk.chunk.citation}
        for source in outcome.evidence_sources:
            assert set(source) == {"kind", "reference", "cited"}

    def test_a_citation_of_nothing_retrieved_falls_back_to_the_criteria(self) -> None:
        """Proves an entirely ungrounded answer is replaced, not stored.

        A negative test: when nothing the model wrote survives the check, the
        analysis must say the prose is deterministic rather than credit the
        model for text that was discarded.
        """
        model = StubLanguageModel(
            scripted_turns=[
                ModelTurn.answering(
                    {
                        "reasons": ["Per [nowhere.md] this is ideal."],
                        "citations": ["nowhere.md"],
                    }
                )
            ]
        )

        outcome = run(build_agent(language_model=model))

        assert outcome.model_name == DETERMINISTIC_FALLBACK_MODEL
        assert outcome.citations == []
        assert not any("nowhere.md" in reason for reason in outcome.reasons)


class TestTheToolManifest:
    """Blueprint section 8: the server's descriptions are what the model reads."""

    def test_both_mcp_tools_appear_with_non_empty_descriptions(self) -> None:
        """Proves the manifest carries the server's own LLM-facing descriptions."""
        definitions, problem = build_tool_manifest(ManifestAwareMcpClient())

        assert problem is None
        by_name = {definition.name: definition for definition in definitions}
        assert set(by_name) == {
            MCP_ADOPTER_TOOL,
            MCP_ANIMAL_TOOL,
            TOOL_RAG_SEARCH,
            TOOL_WEB_SEARCH,
        }
        for definition in definitions:
            assert definition.description.strip()
            assert definition.input_schema["type"] == "object"

    def test_the_local_tools_tell_the_model_the_rag_first_policy(self) -> None:
        """Proves the policy reaches the model as tool documentation, not only code."""
        definitions, _problem = build_tool_manifest(ManifestAwareMcpClient())
        by_name = {definition.name: definition.description for definition in definitions}

        assert "FIRST" in by_name[TOOL_RAG_SEARCH]
        assert "policy-gated" in by_name[TOOL_WEB_SEARCH].lower()
