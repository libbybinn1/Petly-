"""Tests for the Deep Agent engine (requirement 11, blueprint sections 6.2 and 8).

The agent's reasoning loop is `deepagents.create_deep_agent`. These tests
prove the engine is really the one doing the work - the adapter translates
both ways, a scripted model's tool call travels through the deep agent and
its observation comes back, the plan reaches the trace - and that every
failure still degrades to the deterministic explanation.

No live model and no network: every model is a `StubLanguageModel` or a
narrow double, so "the model called rag_search" is a deterministic
assertion (CLAUDE.md R3). Nothing asserts on generated wording.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from agent_service.deep_agent import (
    HIDDEN_BUILTIN_TOOLS,
    MAX_OBSERVATION_CHARACTERS,
    PLANNING_TOOL,
    LanguageModelChatAdapter,
    as_agent_tool,
    as_ai_message,
    as_chat_messages,
    recursion_limit_for,
)
from agent_service.explanation import DETERMINISTIC_FALLBACK_MODEL
from agent_service.llm_client import (
    ChatMessage,
    LanguageModelUnavailableError,
    MalformedModelOutputError,
    ModelTurn,
    StubLanguageModel,
    ToolSchema,
)
from agent_service.loop import TOOL_RAG_SEARCH, load_prompt
from agent_service.tools.mcp_tools import ToolDefinition
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from tests.agent.test_agent_loop import FakeKnowledgeBase, make_chunk
from tests.agent.test_reasoning_loop import actions_in, build_agent, run

pytestmark = pytest.mark.agent

# Anything that names an adoption decision. "adopt" alone is not enough: the
# record tool `get_adopter_profile` contains it and is legitimate.
DECISION_TOOL_PATTERN = re.compile(r"approve|reject|decide|decision|finali[sz]e", re.IGNORECASE)

A_PLAN: dict[str, Any] = {
    "todos": [
        {"content": "check the guide on apartments", "status": "in_progress"},
        {"content": "write the explanation", "status": "pending"},
    ]
}


class UnreachableModel(StubLanguageModel):
    """A stub whose host is down from the first turn."""

    def complete_with_tools(
        self, messages: list[ChatMessage], tools: list[ToolSchema]
    ) -> ModelTurn:
        """Fail as a stopped Ollama host does."""
        raise LanguageModelUnavailableError("connection refused")


class UnparseableModel(StubLanguageModel):
    """A stub that never produces valid JSON."""

    def complete_with_tools(
        self, messages: list[ChatMessage], tools: list[ToolSchema]
    ) -> ModelTurn:
        """Fail as a model that exhausted its JSON retries does."""
        raise MalformedModelOutputError("no JSON object in reply")


class JsonOnlyModel:
    """A model with no tool conversation, only `complete_json`."""

    def __init__(self) -> None:
        """Count how often it is asked."""
        self.call_count = 0

    @property
    def model_name(self) -> str:
        """Identifier for the JSON-only double."""
        return "json-only"

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        """Answer at once with a schema-valid object."""
        self.call_count += 1
        return {"reasons": ["A reason."], "concerns": [], "citations": []}


def offered_names(manifest: list[ToolSchema]) -> set[str]:
    """The tool names one manifest offered."""
    return {entry["function"]["name"] for entry in manifest}


class TestTheAdapterTranslatesBothWays:
    """The adapter is a translation and nothing else."""

    def test_langchain_messages_become_protocol_messages_with_roles(self) -> None:
        """Proves every LangChain message type maps to the role the model expects.

        A tool call is rendered as text naming the tool, and a tool result
        is trimmed, as the hand-written loop did, so the model's view of the
        conversation is unchanged by the engine swap.
        """
        long_observation = "word " * 2000
        converted = as_chat_messages(
            [
                SystemMessage(content="instructions"),
                HumanMessage(content="facts"),
                AIMessage(
                    content="",
                    tool_calls=[{"name": TOOL_RAG_SEARCH, "args": {"query": "q"}, "id": "c1"}],
                ),
                ToolMessage(content=long_observation, tool_call_id="c1"),
                AIMessage(content='{"reasons": []}'),
            ]
        )

        assert [message["role"] for message in converted] == [
            "system",
            "user",
            "assistant",
            "tool",
            "assistant",
        ]
        assert TOOL_RAG_SEARCH in converted[2]["content"]
        assert len(converted[3]["content"]) <= MAX_OBSERVATION_CHARACTERS

    def test_a_tool_turn_becomes_an_ai_message_with_one_tool_call(self) -> None:
        """Proves the model's chosen tool and arguments reach LangGraph intact."""
        message = as_ai_message(
            ModelTurn.calling(TOOL_RAG_SEARCH, {"query": "collies"}), call_identifier="call-7"
        )

        assert len(message.tool_calls) == 1
        assert message.tool_calls[0]["name"] == TOOL_RAG_SEARCH
        assert message.tool_calls[0]["args"] == {"query": "collies"}
        assert message.tool_calls[0]["id"] == "call-7"

    def test_a_final_turn_becomes_parseable_json_content(self) -> None:
        """Proves the final answer survives the round trip as the same object."""
        answer = {"reasons": ["A."], "citations": ["guide.md#x"]}

        message = as_ai_message(ModelTurn.answering(answer), call_identifier="call-1")

        assert message.tool_calls == []
        assert json.loads(message.text) == answer

    def test_bound_tools_reach_the_wrapped_model_as_function_schemas(self) -> None:
        """Proves `bind_tools` hands the protocol the manifest it expects (blueprint 8)."""
        model = StubLanguageModel(
            scripted_turns=[ModelTurn.calling(TOOL_RAG_SEARCH, {"query": "q"})]
        )
        tool = as_agent_tool(
            ToolDefinition(
                name=TOOL_RAG_SEARCH,
                description="Search the guides.",
                input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
            ),
            lambda _arguments: "observed",
        )

        reply = LanguageModelChatAdapter.wrapping(model).bind_tools([tool]).invoke("hello")

        function = model.tool_manifests_seen[0][0]["function"]
        assert function["name"] == TOOL_RAG_SEARCH
        assert function["description"] == "Search the guides."
        assert function["parameters"]["type"] == "object"
        assert isinstance(reply, AIMessage)
        assert reply.tool_calls[0]["name"] == TOOL_RAG_SEARCH

    def test_a_tool_receives_a_misnamed_argument_instead_of_rejecting_it(self) -> None:
        """Proves a small model's `q` instead of `query` is not a wasted turn.

        The project's tools already read the first string argument when
        `query` is missing; LangChain validation must not refuse the call
        before they see it.
        """
        received: list[dict[str, object]] = []
        tool = as_agent_tool(
            ToolDefinition(
                name=TOOL_RAG_SEARCH,
                description="Search.",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            ),
            lambda arguments: received.append(arguments) or "observed",
        )

        observation = tool.invoke({"q": "collies"})

        assert observation == "observed"
        assert received == [{"q": "collies"}]


class TestTheDeepAgentDrivesTheTask:
    """The reasoning runs inside `create_deep_agent`, end to end."""

    def test_a_scripted_tool_call_travels_through_the_deep_agent_and_back(self) -> None:
        """Proves a model-chosen call runs and its observation reaches the session.

        Three facts at once: the query the retriever saw is the model's, the
        trace recorded the call, and the next turn's conversation carried the
        retrieved citation back to the model.
        """
        chunk = make_chunk("space-and-housing.md")
        knowledge_base = FakeKnowledgeBase([chunk])
        model = StubLanguageModel(
            scripted_turns=[
                ModelTurn.calling(TOOL_RAG_SEARCH, {"query": "a deep agent question"}),
                ModelTurn.answering({"reasons": ["A reason."]}),
            ]
        )

        outcome = run(build_agent(language_model=model, knowledge_base=knowledge_base))

        assert "a deep agent question" in knowledge_base.queries
        assert any(
            step.action == TOOL_RAG_SEARCH and "a deep agent question" in step.detail
            for step in outcome.reasoning_trace
        )
        _system_prompt, second_turn_conversation = model.prompts_seen[1]
        assert chunk.chunk.citation in second_turn_conversation

    def test_the_plan_the_model_writes_is_recorded_in_the_trace(self) -> None:
        """Proves staff can see the Deep Agent's plan in the analysis trace.

        The plan here is scripted, so asserting its steps appear is about
        recording, not about generated wording.
        """
        model = StubLanguageModel(
            scripted_turns=[
                ModelTurn.calling(PLANNING_TOOL, A_PLAN),
                ModelTurn.answering({"reasons": ["A reason."]}),
            ]
        )

        outcome = run(build_agent(language_model=model))

        plan_steps = [step for step in outcome.reasoning_trace if step.action == PLANNING_TOOL]
        assert len(plan_steps) == 1
        assert "check the guide on apartments" in plan_steps[0].detail
        assert "in_progress" in plan_steps[0].detail
        assert actions_in(outcome.reasoning_trace)[-1] == "final_answer"

    def test_the_model_receives_the_project_prompts_and_no_builtin_tools(self) -> None:
        """Proves the deep agent is slimmed: our prompts, our tools, planning only.

        A 3B model on a CPU pays for every tool description each turn, so
        the deepagents filesystem and sub-agent tools must not be offered.
        """
        model = StubLanguageModel()

        run(build_agent(language_model=model))

        system_prompt, _conversation = model.prompts_seen[0]
        assert load_prompt("explanation_system.md").strip() in system_prompt
        assert load_prompt("planning_system.md").strip() in system_prompt
        assert offered_names(model.tool_manifests_seen[0]).isdisjoint(HIDDEN_BUILTIN_TOOLS)
        assert PLANNING_TOOL in offered_names(model.tool_manifests_seen[0])

    def test_the_final_answer_is_grounded_before_it_is_stored(self) -> None:
        """Proves an invented citation from the deep agent never reaches storage (R4)."""
        retrieved = make_chunk("space-and-housing.md")
        real_reference = retrieved.chunk.citation
        model = StubLanguageModel(
            scripted_turns=[
                ModelTurn.answering(
                    {
                        "reasons": [
                            f"The home suits this animal [{real_reference}].",
                            "Per [made-up.md] this is ideal.",
                        ],
                        "concerns": [],
                        "missing_information": [],
                        "citations": [real_reference, "made-up.md"],
                    }
                )
            ]
        )

        outcome = run(
            build_agent(language_model=model, knowledge_base=FakeKnowledgeBase([retrieved]))
        )

        assert outcome.citations == [real_reference]
        assert outcome.model_name == model.model_name
        assert not any("made-up.md" in reason for reason in outcome.reasons)

    def test_a_json_only_model_still_completes_through_the_deep_agent(self) -> None:
        """Proves the adapter keeps `next_model_turn`'s graceful degradation."""
        model = JsonOnlyModel()

        outcome = run(build_agent(language_model=model))

        assert model.call_count == 1
        assert outcome.model_name == "json-only"
        assert "final_answer" in actions_in(outcome.reasoning_trace)


class TestEveryFailureDegradesToTheScore:
    """FR-10.10: a run that cannot finish costs the prose, never the analysis."""

    def test_the_step_cap_stops_a_model_that_only_calls_tools(self) -> None:
        """Proves `max_reasoning_steps` bounds the deep agent's model calls exactly.

        A negative test: planning counts against the same budget, so a model
        that plans and then only searches is stopped after three turns.
        """
        model = StubLanguageModel(
            scripted_turns=[
                ModelTurn.calling(PLANNING_TOOL, A_PLAN),
                *(
                    ModelTurn.calling(TOOL_RAG_SEARCH, {"query": f"question {index}"})
                    for index in range(8)
                ),
            ]
        )

        outcome = run(build_agent(language_model=model, max_reasoning_steps=3))

        assert model.turns_taken == 3
        assert "step_cap_reached" in actions_in(outcome.reasoning_trace)
        assert outcome.model_name == DETERMINISTIC_FALLBACK_MODEL
        assert outcome.reasons or outcome.concerns
        assert any("budget" in item for item in outcome.missing_information)

    def test_an_unreachable_model_falls_back_to_the_deterministic_explanation(self) -> None:
        """Proves an Ollama outage inside the deep agent is observed, not raised."""
        outcome = run(build_agent(language_model=UnreachableModel()))

        assert "model_unavailable" in actions_in(outcome.reasoning_trace)
        assert outcome.model_name == DETERMINISTIC_FALLBACK_MODEL
        assert 0 <= outcome.score.score <= 100

    def test_a_malformed_answer_falls_back_to_the_deterministic_explanation(self) -> None:
        """Proves unparseable output inside the deep agent is observed, not raised."""
        outcome = run(build_agent(language_model=UnparseableModel()))

        assert "malformed_answer" in actions_in(outcome.reasoning_trace)
        assert outcome.model_name == DETERMINISTIC_FALLBACK_MODEL

    def test_the_recursion_backstop_never_preempts_the_step_cap(self) -> None:
        """Proves LangGraph's limit sits above what `max_reasoning_steps` turns need.

        Measured: one turn and its tool call take three supersteps, plus two
        to enter and leave the graph. A backstop below that would end runs
        early with the wrong trace entry.
        """
        for step_budget in range(1, 13):
            assert recursion_limit_for(step_budget) > 3 * step_budget + 2


class TestTheAgentCannotDecide:
    """CLAUDE.md R4: the agent scores, explains and recommends; a human decides."""

    def test_no_tool_offered_to_the_model_names_a_decision(self) -> None:
        """Proves the deep agent's manifest holds no decision tool (negative test)."""
        model = StubLanguageModel(
            scripted_turns=[
                ModelTurn.calling(TOOL_RAG_SEARCH, {"query": "anything"}),
                ModelTurn.answering({"reasons": ["A reason."]}),
            ]
        )

        run(build_agent(language_model=model))

        for manifest in model.tool_manifests_seen:
            decision_tools = [
                name for name in offered_names(manifest) if DECISION_TOOL_PATTERN.search(name)
            ]
            assert decision_tools == []

    @pytest.mark.parametrize("invented_tool", ["approve_adoption", "write_file", "task"])
    def test_calling_a_decision_or_hidden_tool_is_refused_and_recorded(
        self, invented_tool: str
    ) -> None:
        """Proves neither an invented decision tool nor a withheld built-in runs.

        A negative test: the call is reported back to the model as unknown,
        the trace records it, and the analysis still completes with a score.
        """
        model = StubLanguageModel(
            scripted_turns=[
                ModelTurn.calling(invented_tool, {"path": "/x", "description": "y"}),
                ModelTurn.answering({"reasons": ["A reason."]}),
            ]
        )

        outcome = run(build_agent(language_model=model))

        assert any(
            step.action == "unknown_tool" and step.detail == invented_tool
            for step in outcome.reasoning_trace
        )
        assert "final_answer" in actions_in(outcome.reasoning_trace)
        assert 0 <= outcome.score.score <= 100

