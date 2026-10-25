"""The Deep Agent engine that drives the agent's reasoning.

Requirement 11 of the course brief asks for the candidate-assessment agent to
be built on LangChain's **Deep Agent** infrastructure, and blueprint section 6
asks for a real plan-act-observe agent rather than one model call. This module
is where both are met: the reasoning loop is `deepagents.create_deep_agent`,
running on LangGraph, with its `write_todos` planning tool.

Three pieces live here, each kept narrow:

- `LanguageModelChatAdapter` - a LangChain chat model that wraps the project's
  own `LanguageModel` protocol. The deep agent therefore talks to the same
  `OllamaLanguageModel` the rest of the agent uses, and a test drives it with
  `StubLanguageModel(scripted_turns=...)` and no model at all (CLAUDE.md R3).
  `langchain-ollama` is deliberately not used: it would be a second Ollama
  client with its own retry and JSON handling, and it could not be scripted.
- `ReasoningTraceMiddleware` - copies what the deep agent does that the tools
  themselves cannot see (its plan, and any tool it invents) into the
  `ReasoningSession`, so staff read one trace (blueprint section 6.2).
- `run_deep_agent` - builds the agent for one task, runs it under a hard step
  cap, and turns every way a run can fail into the same trace entries the
  hand-written loop used to write, so a failure degrades to the
  deterministic explanation rather than failing the job (FR-10.10).

What the deep agent is *not* given matters as much (docs/AGENT.md section 4):
no filesystem tools, no sub-agents, no summarization and no decision tool.
CLAUDE.md R4 - the agent scores, explains and recommends; a human decides.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from langchain.agents.middleware import (
    AgentMiddleware,
    AgentState,
    ModelCallLimitMiddleware,
    TodoListMiddleware,
)
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import LangSmithParams, LanguageModelInput
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.tool import tool_call as build_tool_call
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool, StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.errors import GraphRecursionError
from langgraph.prebuilt.tool_node import ToolCallRequest as GraphToolCallRequest
from langgraph.types import Command
from pydantic import PrivateAttr

from agent_service.explanation import shorten
from agent_service.llm_client import (
    ChatMessage,
    JsonCompletion,
    LanguageModelUnavailableError,
    MalformedModelOutputError,
    ModelTurn,
    ToolCallRequest,
    ToolSchema,
    next_model_turn,
)
from agent_service.reasoning_session import ReasoningSession
from agent_service.tools.mcp_tools import ToolDefinition

# The provider name the adapter reports to deepagents. Its harness profile is
# registered under this key, which is how the slimming below is applied to
# this model and to nothing else in the process.
PETMATCH_MODEL_PROVIDER = "petmatch"

# The Deep Agent planning tool, contributed by `TodoListMiddleware`.
PLANNING_TOOL = "write_todos"

PLANNING_TOOL_DESCRIPTION = (
    "Record your plan for this assessment as a short list of steps, each with "
    "a status of pending, in_progress or completed. Each call replaces the "
    "whole list, so send every step each time."
)

# Built-in deepagents tools withheld from the model. The deep agent's
# FilesystemMiddleware cannot be removed (deepagents treats it as protected
# scaffolding), but every tool it contributes can be hidden. None has a job
# here: the agent reads records through MCP and guidance through RAG, and a
# 3B model on a CPU pays for every tool description on every turn.
HIDDEN_BUILTIN_TOOLS = frozenset(
    {"ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "execute", "task"}
)

# Observations are fed back to a 3-billion-parameter model with a finite
# context window, so each one is trimmed rather than pasted whole.
MAX_OBSERVATION_CHARACTERS = 1200
MAX_TRACE_DETAIL_CHARACTERS = 200
MAX_PLAN_IN_TRACE_CHARACTERS = 400

# Measured: one model turn plus its tool call is three LangGraph supersteps,
# with two more spent entering and leaving the graph. The recursion limit is
# a backstop set comfortably above that, so the model-call limit - which
# counts exactly what `max_reasoning_steps` means - is what normally stops a
# run, and the recursion limit only ever catches a runaway graph.
SUPERSTEPS_PER_TURN_BACKSTOP = 4
SUPERSTEP_HEADROOM = 8


def _register_petmatch_harness_profile() -> None:
    """Slim the deep agent to what a 3B model on a CPU can afford.

    Each exclusion is deliberate (docs/AGENT.md section 4, CLAUDE.md
    performance table):

    - **Filesystem tools hidden.** Nothing here reads or writes files.
    - **General-purpose sub-agent disabled**, which also removes the `task`
      tool. A sub-agent is a second agent loop at 30-40 seconds a turn; the
      job is one pairing, small enough for one context.
    - **Summarization dropped.** The step cap bounds the conversation far
      below the context window, and a summary would be another model call
      outside the step budget.

    What remains is the Deep Agent planning tool (`write_todos`), the tool-call
    patching middleware, and the provider prompt-caching middleware, which
    no-ops for a non-Anthropic model.
    """
    register_harness_profile(
        PETMATCH_MODEL_PROVIDER,
        HarnessProfile(
            excluded_tools=HIDDEN_BUILTIN_TOOLS,
            excluded_middleware=frozenset({"SummarizationMiddleware"}),
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )


_register_petmatch_harness_profile()


# --------------------------------------------------------------------------
# The chat-model adapter
# --------------------------------------------------------------------------


class LanguageModelChatAdapter(BaseChatModel):
    """A LangChain chat model backed by the project's `LanguageModel` protocol.

    The deep agent speaks LangChain messages; the project's models speak
    `ChatMessage` dicts and `ModelTurn`s. This class is only the translation
    between the two, so the model that actually answers - Ollama in
    production, a scripted stub in tests - is unchanged (blueprint section
    6.2, CLAUDE.md R3).
    """

    model_name: str
    _language_model: JsonCompletion = PrivateAttr()

    @classmethod
    def wrapping(cls, language_model: JsonCompletion) -> LanguageModelChatAdapter:
        """Wrap one project model.

        A factory rather than `__init__`, because pydantic owns the
        constructor's signature and the wrapped model is private state, not
        a validated field.

        Args:
            language_model: The model to ask for each turn. One that can
                hold a tool conversation is asked with the manifest; one
                that only speaks JSON answers at once (see
                `next_model_turn`).

        Returns:
            The adapter, reporting the wrapped model's name.
        """
        adapter = cls.model_validate({"model_name": language_model.model_name})
        adapter._language_model = language_model
        return adapter

    @property
    def _llm_type(self) -> str:
        """The adapter's type tag, used by LangChain for tracing."""
        return "petmatch-language-model"

    def _get_ls_params(
        self,
        stop: list[str] | None = None,
        **kwargs: Any,  # noqa: ANN401 - signature fixed by BaseChatModel
    ) -> LangSmithParams:
        """Report the PetMatch provider, which selects its harness profile."""
        params = super()._get_ls_params(stop=stop, **kwargs)
        params["ls_provider"] = PETMATCH_MODEL_PROVIDER
        params["ls_model_name"] = self.model_name
        return params

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,  # noqa: ARG002 - the model always chooses freely
        **kwargs: Any,  # noqa: ANN401 - signature fixed by BaseChatModel
    ) -> Runnable[LanguageModelInput, AIMessage]:
        """Attach a tool manifest, converted to the schema the protocol takes.

        `tool_choice` is ignored on purpose: forcing a tool would take the
        decision away from the model, which blueprint section 6.2 gives to
        it.

        Args:
            tools: The tools the deep agent offers on this turn.
            tool_choice: Ignored.
            **kwargs: Further settings; ignored by the project's models.

        Returns:
            This model, bound to the converted manifest.
        """
        del kwargs
        return self.bind(tools=[as_tool_schema(tool) for tool in tools])

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,  # noqa: ARG002 - the protocol has no stop words
        run_manager: CallbackManagerForLLMRun | None = None,  # noqa: ARG002 - nothing streams
        **kwargs: Any,  # noqa: ANN401 - signature fixed by BaseChatModel
    ) -> ChatResult:
        """Ask the wrapped model for its next turn.

        Args:
            messages: The conversation the deep agent assembled.
            stop: Unused.
            run_manager: Unused.
            **kwargs: Carries `tools`, the manifest bound by `bind_tools`.

        Returns:
            One generation: a tool call, or the final answer as JSON text.

        Raises:
            LanguageModelUnavailableError: The model host is unreachable.
            MalformedModelOutputError: The model never produced valid JSON.
        """
        manifest = _manifest_from(kwargs.get("tools"))
        turn = next_model_turn(self._language_model, as_chat_messages(messages), manifest)
        reply = as_ai_message(turn, call_identifier=f"call-{len(messages)}")
        return ChatResult(generations=[ChatGeneration(message=reply)])


def as_chat_messages(messages: Sequence[BaseMessage]) -> list[ChatMessage]:
    """Convert a LangChain conversation into the protocol's message dicts.

    Args:
        messages: The deep agent's conversation, oldest first.

    Returns:
        The same conversation as `ChatMessage` dicts.
    """
    return [as_chat_message(message) for message in messages]


def as_chat_message(message: BaseMessage) -> ChatMessage:
    """Convert one LangChain message into a role and text.

    A tool call is rendered as a line of text, as the hand-written loop did,
    because `ChatMessage` carries text only; the tool result that follows is
    trimmed so one long passage cannot crowd the rest out of a small model's
    context.

    Args:
        message: One message from the deep agent's conversation.

    Returns:
        The message as the protocol understands it.
    """
    if isinstance(message, SystemMessage):
        return {"role": "system", "content": message.text}
    if isinstance(message, ToolMessage):
        return {"role": "tool", "content": shorten(message.text, MAX_OBSERVATION_CHARACTERS)}
    if isinstance(message, AIMessage):
        return {"role": "assistant", "content": _assistant_text(message)}
    return {"role": "user", "content": message.text}


def _assistant_text(message: AIMessage) -> str:
    """Render an assistant turn, naming the tool it called if it called one."""
    if not message.tool_calls:
        return message.text
    first_call = message.tool_calls[0]
    request = ToolCallRequest(tool_name=first_call["name"], arguments=dict(first_call["args"]))
    return f"Calling {request.tool_name}({request.argument_summary})."


def as_ai_message(turn: ModelTurn, call_identifier: str) -> AIMessage:
    """Convert a `ModelTurn` into the message the deep agent expects.

    Args:
        turn: The wrapped model's reply.
        call_identifier: An identifier for the tool call, unique within the
            conversation, so its result can be matched back to it.

    Returns:
        An assistant message carrying either one tool call, or the final
        answer serialised as a JSON object.
    """
    if turn.tool_call is not None:
        call = build_tool_call(
            name=turn.tool_call.tool_name,
            args=dict(turn.tool_call.arguments),
            id=call_identifier,
        )
        return AIMessage(content="", tool_calls=[call])
    return AIMessage(content=json.dumps(turn.final_content or {}, ensure_ascii=False))


def as_tool_schema(tool: Mapping[str, Any] | type | Callable[..., Any] | BaseTool) -> ToolSchema:
    """Convert a LangChain tool into the function schema the protocol takes.

    This is the same `{"type": "function", "function": {...}}` shape Ollama's
    chat API reads, so the manifest the model sees is the one each tool
    declares (blueprint section 8).

    Args:
        tool: A tool as the deep agent holds it.

    Returns:
        The tool's function schema.
    """
    return convert_to_openai_tool(tool)


def _manifest_from(raw_tools: object) -> list[ToolSchema]:
    """Read the bound manifest back out of the generation settings."""
    if not isinstance(raw_tools, list):
        return []
    return [dict(entry) for entry in raw_tools if isinstance(entry, dict)]


def as_agent_tool(
    definition: ToolDefinition, run_tool: Callable[[dict[str, object]], str]
) -> BaseTool:
    """Expose one project tool to the deep agent.

    The definition's own JSON schema is used as-is, so an MCP tool keeps the
    description and arguments its server advertised at runtime (docs/MCP.md
    section 5). A JSON-schema tool is not validated by LangChain, which is
    intended: the project's tools already tolerate a small model naming an
    argument `q` instead of `query`, and rejecting that call would waste a
    turn.

    Args:
        definition: The tool's name, description and input schema.
        run_tool: Called with the arguments the model chose; returns the
            observation the model will read.

    Returns:
        A LangChain tool the deep agent can offer and call.
    """

    def invoke_tool(**arguments: object) -> str:
        return run_tool(dict(arguments))

    return StructuredTool.from_function(
        func=invoke_tool,
        name=definition.name,
        description=definition.description,
        args_schema=definition.input_schema,
        infer_schema=False,
    )


# --------------------------------------------------------------------------
# Recording the deep agent's own actions in the trace
# --------------------------------------------------------------------------


class ReasoningTraceMiddleware(AgentMiddleware[AgentState[Any], None, Any]):
    """Copies the deep agent's plan and invented tools into the trace.

    The project's tools record themselves. What they cannot see is the
    planning tool, which belongs to the deep agent, and a call to a tool
    that does not exist. Both are recorded here so the trace shows staff
    the plan the agent made and every step it took (blueprint section 6.2).
    """

    def __init__(self, session: ReasoningSession) -> None:
        """Record into one task's session.

        Args:
            session: The task state that receives the trace entries.
        """
        super().__init__()
        self._session = session

    def wrap_tool_call(
        self,
        request: GraphToolCallRequest,
        handler: Callable[[GraphToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        """Record a plan or an invented tool around one tool call.

        Args:
            request: The call the model made.
            handler: Runs the call.

        Returns:
            The tool's result, or a refusal naming the invented tool so the
            model can re-plan instead of the run failing.
        """
        tool_name = request.tool_call["name"]
        if request.tool is None or tool_name in HIDDEN_BUILTIN_TOOLS:
            self._session.record("unknown_tool", tool_name)
            return ToolMessage(
                content=(
                    f"There is no tool named {tool_name}. Choose one of the tools "
                    f"you were given, or answer with your final JSON object."
                ),
                tool_call_id=request.tool_call["id"] or tool_name,
                name=tool_name,
                status="error",
            )

        result = handler(request)
        if tool_name == PLANNING_TOOL:
            self._session.record(PLANNING_TOOL, _describe_plan(request.tool_call["args"], result))
        return result


def _describe_plan(arguments: Mapping[str, Any], result: ToolMessage | Command[Any]) -> str:
    """Render a plan for the trace, or say that it was rejected."""
    if isinstance(result, ToolMessage) and result.status == "error":
        return shorten(f"plan rejected: {result.text}", MAX_TRACE_DETAIL_CHARACTERS)

    todos = arguments.get("todos")
    if not isinstance(todos, list) or not todos:
        return "empty plan"
    steps = [
        f"{index}. [{todo.get('status', 'pending')}] {todo.get('content', '')}"
        for index, todo in enumerate(todos, start=1)
        if isinstance(todo, Mapping)
    ]
    return shorten("; ".join(steps), MAX_PLAN_IN_TRACE_CHARACTERS)


# --------------------------------------------------------------------------
# Running one task
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DeepAgentTask:
    """Everything the deep agent needs for one analysis.

    Attributes:
        system_prompt: The explanation instructions, from a prompt file.
        planning_prompt: The `write_todos` guidance, from a prompt file.
        user_prompt: The facts, the score and the evidence so far.
        tools: The project tools the model may choose from.
        max_reasoning_steps: The most model turns the run may take.
    """

    system_prompt: str
    planning_prompt: str
    user_prompt: str
    tools: list[BaseTool]
    max_reasoning_steps: int


def run_deep_agent(
    language_model: JsonCompletion, task: DeepAgentTask, session: ReasoningSession
) -> dict[str, Any] | None:
    """Run the deep agent for one task and return the model's final answer.

    The step cap is `max_reasoning_steps` model calls, enforced by
    `ModelCallLimitMiddleware`; LangGraph's `recursion_limit`, derived from
    the same number, is the backstop (blueprint section 6.2: the loop
    terminates by construction).

    Args:
        language_model: The model that answers each turn.
        task: The prompts, tools and step budget.
        session: The task state, which receives every trace entry.

    Returns:
        The model's final JSON answer, or None when it produced none - the
        host was unreachable, the answer never parsed, or the step budget ran
        out. Each case is recorded in the trace before returning.
    """
    agent = create_deep_agent(
        model=LanguageModelChatAdapter.wrapping(language_model),
        tools=task.tools,
        system_prompt=task.system_prompt,
        middleware=_middleware_for(task, session),
        name="petmatch-match-analysis",
    )
    try:
        final_state = agent.invoke(
            {"messages": [HumanMessage(content=task.user_prompt)]},
            config={"recursion_limit": recursion_limit_for(task.max_reasoning_steps)},
        )
    except LanguageModelUnavailableError as error:
        session.record("model_unavailable", _trace_detail(error))
        return None
    except MalformedModelOutputError as error:
        session.record("malformed_answer", _trace_detail(error))
        return None
    except (ModelCallLimitExceededError, GraphRecursionError):
        _record_step_cap(task.max_reasoning_steps, session)
        return None

    return _final_answer(final_state.get("messages", []), session)


def _middleware_for(
    task: DeepAgentTask, session: ReasoningSession
) -> list[AgentMiddleware[Any, Any, Any]]:
    """The middleware added to the deep agent's own stack, in order.

    Args:
        task: The task, for its planning prompt and step budget.
        session: The task state the trace middleware records into.

    Returns:
        Planning (the Deep Agent `write_todos` tool), the step cap, and the
        trace recorder.
    """
    return [
        TodoListMiddleware(
            system_prompt=task.planning_prompt, tool_description=PLANNING_TOOL_DESCRIPTION
        ),
        ModelCallLimitMiddleware(run_limit=task.max_reasoning_steps, exit_behavior="error"),
        ReasoningTraceMiddleware(session),
    ]


def recursion_limit_for(max_reasoning_steps: int) -> int:
    """LangGraph's superstep limit for a run of `max_reasoning_steps` turns.

    Args:
        max_reasoning_steps: The model-turn budget.

    Returns:
        A limit the model-call cap is always reached before.
    """
    return SUPERSTEPS_PER_TURN_BACKSTOP * max(max_reasoning_steps, 1) + SUPERSTEP_HEADROOM


def _final_answer(
    messages: Sequence[BaseMessage], session: ReasoningSession
) -> dict[str, Any] | None:
    """Read the final answer out of the finished conversation."""
    model_turn_count = sum(1 for message in messages if isinstance(message, AIMessage))
    last_message = messages[-1] if messages else None
    if not isinstance(last_message, AIMessage):
        session.record("malformed_answer", "the deep agent ended without an answer")
        return None

    try:
        answer = json.loads(last_message.text)
    except json.JSONDecodeError as error:
        session.record("malformed_answer", _trace_detail(error))
        return None
    if not isinstance(answer, dict):
        session.record("malformed_answer", f"answer was a {type(answer).__name__}")
        return None

    session.record("final_answer", f"step {model_turn_count}: keys={sorted(answer)}")
    return answer


def _record_step_cap(max_reasoning_steps: int, session: ReasoningSession) -> None:
    """Record that the budget ran out before the model answered."""
    session.record("step_cap_reached", f"stopped after {max_reasoning_steps} step(s)")
    session.note_gap(
        f"The agent reached its {max_reasoning_steps}-step reasoning "
        f"budget before writing an explanation."
    )


def _trace_detail(error: Exception) -> str:
    """Render one exception short enough to sit in the reasoning trace."""
    return shorten(str(error), MAX_TRACE_DETAIL_CHARACTERS)
