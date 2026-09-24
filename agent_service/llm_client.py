"""Language-model access for the agent.

The model has two jobs and neither is arithmetic: interpreting
natural-language intent, and deciding what to look up before writing an
explanation. It never produces a score - that is deterministic Python, per
spec section 8 and docs/AGENT.md section 1.

Two capabilities are exposed, because the agent needs two shapes of call:

- `complete_json` - one prompt in, one JSON object out. Used by the intent
  interpreter (spec section 6.3), which has nothing to look up.
- `complete_with_tools` - a whole conversation plus a tool manifest in, and
  either *a tool the model chose to call* or its final JSON answer out. This
  is what makes the reason-act loop real rather than a single call
  (blueprint section 6.2).

Both are wrapped behind a protocol so the agent depends on the capability
rather than on Ollama, and so tests can substitute a deterministic stand-in
and run with no model installed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# A model occasionally emits prose around its JSON, or truncates it. Retrying
# with a firmer instruction usually fixes it; retrying forever would hang the
# queue, so the attempts are bounded.
MAX_JSON_ATTEMPTS = 3

# One conversation turn as both Ollama and the stub understand it: a role and
# text. Tool results are fed back as `{"role": "tool", "content": ...}`.
ChatMessage = dict[str, str]

# One tool as Ollama's chat API expects it, built from a ToolDefinition.
ToolSchema = dict[str, Any]


class LanguageModelUnavailableError(RuntimeError):
    """Raised when the model cannot be reached at all."""


class MalformedModelOutputError(ValueError):
    """Raised when the model never produced parseable JSON."""


@dataclass(frozen=True)
class ToolCallRequest:
    """A tool the model asked for, with the arguments it chose itself.

    Blueprint section 6.2 requires the agent to decide *whether and which*
    tool to invoke. This type is that decision, made by the model rather than
    hardcoded by the caller.
    """

    tool_name: str
    arguments: dict[str, Any]

    @property
    def argument_summary(self) -> str:
        """A short, loggable rendering of the arguments for the trace."""
        return ", ".join(f"{name}={value!r}" for name, value in sorted(self.arguments.items()))


@dataclass(frozen=True)
class ModelTurn:
    """One reply from a tool-using model.

    Exactly one of the two fields is populated: the model either asks for a
    tool, or answers. Keeping both in one type lets the loop treat "act" and
    "answer" as the same kind of event and decide what to do next.
    """

    tool_call: ToolCallRequest | None = None
    final_content: dict[str, Any] | None = None

    @property
    def is_final(self) -> bool:
        """Whether this turn is the model's answer rather than a tool call."""
        return self.tool_call is None

    @classmethod
    def calling(cls, tool_name: str, arguments: dict[str, Any]) -> ModelTurn:
        """Build a turn that asks for one tool call."""
        return cls(tool_call=ToolCallRequest(tool_name=tool_name, arguments=arguments))

    @classmethod
    def answering(cls, content: dict[str, Any]) -> ModelTurn:
        """Build a turn that carries the model's final JSON answer."""
        return cls(final_content=dict(content))


class JsonCompletion(Protocol):
    """Anything that can answer one prompt with a JSON object.

    The narrower of the two capabilities, declared separately because the
    intent interpreter needs only this. Asking it to depend on tool calling
    as well would be asking for something it never uses.
    """

    @property
    def model_name(self) -> str:
        """Identifier recorded on each analysis for auditability."""
        ...

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        """Return a parsed JSON object for the given prompts."""
        ...


class LanguageModel(JsonCompletion, Protocol):
    """A model that can also hold a tool-using conversation.

    What the agent loop requires: the loop's whole purpose is to let the
    model choose tools, so a model that cannot be offered any is not enough
    for it - even though `next_model_turn` degrades gracefully when given
    one.
    """

    def complete_with_tools(
        self, messages: list[ChatMessage], tools: list[ToolSchema]
    ) -> ModelTurn:
        """Return the model's next move: a tool call, or its final answer."""
        ...


@runtime_checkable
class ToolConversation(Protocol):
    """The optional half of a model: being able to hold a tool conversation.

    A runtime-checkable protocol rather than a `getattr` probe, so the
    capability has a name and a signature the type checker can see. The probe
    asked only whether *something callable* was there, which a stub with the
    wrong arity would have satisfied - and the failure then surfaced as a
    TypeError inside the loop rather than as the graceful degradation this
    function promises.
    """

    def complete_with_tools(
        self, messages: list[ChatMessage], tools: list[ToolSchema]
    ) -> ModelTurn:
        """Return the model's next move: a tool call, or its final answer."""
        ...


def next_model_turn(
    language_model: JsonCompletion, messages: list[ChatMessage], tools: list[ToolSchema]
) -> ModelTurn:
    """Ask a model for its next turn, tolerating a model that only speaks JSON.

    The tool-using call is the newer of the two capabilities (blueprint
    section 6.2). A model that predates it - or a narrow test double that
    implements only `complete_json` - is still usable: its single JSON reply
    is treated as an immediate final answer, which is exactly the one-call
    behaviour the loop degrades to.

    Args:
        language_model: The model to ask.
        messages: The conversation so far, oldest first.
        tools: The manifest the model may choose from.

    Returns:
        The model's next turn.

    Raises:
        LanguageModelUnavailableError: The model host is unreachable.
        MalformedModelOutputError: No attempt produced valid JSON.
    """
    if isinstance(language_model, ToolConversation):
        turn = language_model.complete_with_tools(messages, tools)
        if isinstance(turn, ModelTurn):
            return turn
        raise MalformedModelOutputError(
            f"complete_with_tools returned {type(turn).__name__}, expected a ModelTurn"
        )

    system_prompt, user_prompt = split_conversation(messages)
    return ModelTurn(final_content=language_model.complete_json(system_prompt, user_prompt))


def split_conversation(messages: list[ChatMessage]) -> tuple[str, str]:
    """Flatten a conversation into a system prompt and everything else.

    Used when talking to a JSON-only model, and by the test stub, so that one
    conversation can be inspected as the pair of prompts the older interface
    took.

    Args:
        messages: The conversation so far.

    Returns:
        The joined system content, and the joined remaining content.
    """
    system_parts = [
        message.get("content", "") for message in messages if message.get("role") == "system"
    ]
    other_parts = [
        message.get("content", "") for message in messages if message.get("role") != "system"
    ]
    return "\n\n".join(system_parts), "\n\n".join(other_parts)


@dataclass
class OllamaLanguageModel:
    """A locally hosted model served by Ollama."""

    base_url: str
    name: str
    temperature: float = 0.0

    @property
    def model_name(self) -> str:
        """The model identifier, recorded on every analysis."""
        return self.name

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        """Prompt the model and parse its reply as JSON.

        Uses Ollama's JSON mode, which constrains decoding, and still retries
        on a parse failure because constrained decoding is not a guarantee.

        Args:
            system_prompt: The instructions, from a versioned prompt file.
            user_prompt: The material the model may reason from.

        Returns:
            The parsed object.

        Raises:
            LanguageModelUnavailableError: The model host is unreachable.
            MalformedModelOutputError: No attempt produced valid JSON.
        """
        last_error = ""

        for attempt in range(1, MAX_JSON_ATTEMPTS + 1):
            prompt = user_prompt if attempt == 1 else _stricter(user_prompt, last_error)
            reply = self._chat(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                tools=[],
                force_json=True,
            )
            try:
                return _json_object(str(reply.get("content") or ""))
            except MalformedModelOutputError as error:
                last_error = str(error)

        raise MalformedModelOutputError(
            f"{self.name} produced no valid JSON object in {MAX_JSON_ATTEMPTS} attempts: "
            f"{last_error}"
        )

    def complete_with_tools(
        self, messages: list[ChatMessage], tools: list[ToolSchema]
    ) -> ModelTurn:
        """Let the model choose a tool, or answer, using Ollama tool calling.

        Implements the model side of blueprint section 6.2: the manifest is
        sent with every turn, so the decision about which tool to call - or
        whether any is still needed - is the model's.

        JSON mode is deliberately *not* forced here: on this stack it
        suppresses tool calls. The final answer is therefore parsed
        leniently, and re-asked more firmly if it will not parse, up to
        `MAX_JSON_ATTEMPTS`.

        Args:
            messages: The conversation so far, oldest first.
            tools: The tool manifest the model may choose from.

        Returns:
            The tool the model chose, or its final answer.

        Raises:
            LanguageModelUnavailableError: The model host is unreachable.
            MalformedModelOutputError: The answer never parsed as JSON.
        """
        last_error = ""

        for attempt in range(1, MAX_JSON_ATTEMPTS + 1):
            sent = messages if attempt == 1 else _with_stricter_instruction(messages, last_error)
            reply = self._chat(sent, tools=tools, force_json=False)

            requested = _first_tool_call(reply) or _tool_call_written_as_content(reply, tools)
            if requested is not None:
                return ModelTurn(tool_call=requested)

            try:
                return ModelTurn(final_content=_json_object(str(reply.get("content") or "")))
            except MalformedModelOutputError as error:
                last_error = str(error)

        raise MalformedModelOutputError(
            f"{self.name} produced no valid JSON answer in {MAX_JSON_ATTEMPTS} attempts: "
            f"{last_error}"
        )

    def _chat(
        self, messages: list[ChatMessage], *, tools: list[ToolSchema], force_json: bool
    ) -> dict[str, Any]:
        """Send one chat request and return the assistant message.

        Args:
            messages: The conversation to send.
            tools: The manifest, empty when the call needs no tools.
            force_json: Whether to constrain decoding to JSON.

        Returns:
            The assistant message as a mapping.

        Raises:
            LanguageModelUnavailableError: The host could not be reached.
            MalformedModelOutputError: The reply had no assistant message.
        """
        import ollama

        client = ollama.Client(host=self.base_url)
        request: dict[str, Any] = {
            "model": self.name,
            "messages": messages,
            "options": {"temperature": self.temperature},
        }
        if tools:
            request["tools"] = tools
        if force_json:
            request["format"] = "json"

        try:
            response = client.chat(**request)
        except Exception as error:  # - surfaced as a typed failure
            raise LanguageModelUnavailableError(
                f"Could not reach {self.name} at {self.base_url}: {error}"
            ) from error

        if not isinstance(response, Mapping):
            raise MalformedModelOutputError(
                f"{self.name} returned {type(response).__name__}, expected a mapping"
            )

        message = response.get("message")
        if not isinstance(message, Mapping):
            raise MalformedModelOutputError(f"{self.name} returned no assistant message")
        return dict(message)


def _first_tool_call(message: Mapping[str, Any]) -> ToolCallRequest | None:
    """Read the first tool call out of an assistant message, if it made one.

    Only the first is taken. One tool per turn keeps each observation
    attributable to the call that produced it, which is what makes the
    reasoning trace readable.

    Args:
        message: The assistant message from Ollama.

    Returns:
        The requested call, or None when the model answered instead.
    """
    raw_calls = message.get("tool_calls") or []
    if not isinstance(raw_calls, list) or not raw_calls:
        return None

    first = raw_calls[0]
    function = first.get("function") if isinstance(first, Mapping) else None
    if not isinstance(function, Mapping):
        return None

    tool_name = str(function.get("name") or "").strip()
    if not tool_name:
        return None
    return ToolCallRequest(tool_name=tool_name, arguments=_argument_mapping(function))


def _tool_call_written_as_content(
    message: Mapping[str, Any], tools: list[ToolSchema]
) -> ToolCallRequest | None:
    """Recognise a tool call the model wrote as content instead of a tool call.

    Observed with `qwen2.5:3b-instruct`: having called a tool correctly once,
    it sometimes writes `{"name": "rag_search", "arguments": {...}}` as
    ordinary content. Treating that as a final answer would waste a turn and
    then be discarded by the grounding check, so it is honoured as the call
    it plainly is - but only when the name matches a tool that was actually
    offered, so a real answer can never be mistaken for one.

    Args:
        message: The assistant message from Ollama.
        tools: The manifest that was offered on this turn.

    Returns:
        The call the content described, or None.
    """
    offered = _offered_tool_names(tools)
    if not offered:
        return None

    try:
        parsed = _json_object(str(message.get("content") or ""))
    except MalformedModelOutputError:
        return None

    tool_name = str(parsed.get("name") or "").strip()
    if tool_name not in offered:
        return None
    return ToolCallRequest(tool_name=tool_name, arguments=_argument_mapping(parsed))


def _offered_tool_names(tools: list[ToolSchema]) -> set[str]:
    """The names in a manifest, as the model was shown them."""
    names: set[str] = set()
    for entry in tools:
        function = entry.get("function")
        if isinstance(function, Mapping):
            names.add(str(function.get("name") or ""))
    return {name for name in names if name}


def _argument_mapping(function: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise a tool call's arguments, which may arrive as a JSON string."""
    raw_arguments = function.get("arguments")
    if raw_arguments is None:
        raw_arguments = function.get("parameters")
    if isinstance(raw_arguments, Mapping):
        return {str(name): value for name, value in raw_arguments.items()}

    if isinstance(raw_arguments, str):
        try:
            decoded = json.loads(raw_arguments)
        except json.JSONDecodeError:
            return {}
        if isinstance(decoded, dict):
            return {str(name): value for name, value in decoded.items()}

    return {}


def _json_object(raw_text: str) -> dict[str, Any]:
    """Parse a model reply as a JSON object, tolerating text around it.

    Small models wrap JSON in prose or a markdown fence even when told not
    to. Recovering the object is better than failing the analysis over
    punctuation, and the recovery is bounded: the first `{` to the last `}`,
    parsed or refused.

    Args:
        raw_text: Whatever the model said.

    Returns:
        The parsed object.

    Raises:
        MalformedModelOutputError: No JSON object could be read.
    """
    candidates = [raw_text.strip()]
    enclosed = re.search(r"\{.*\}", raw_text, re.DOTALL)
    if enclosed is not None:
        candidates.append(enclosed.group(0))

    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    raise MalformedModelOutputError(f"no JSON object in reply: {raw_text[:200]!r}")


def _stricter(original_prompt: str, previous_error: str) -> str:
    """Re-ask more firmly after an unparseable reply."""
    return (
        f"{original_prompt}\n\n"
        f"Your previous reply could not be parsed ({previous_error}). "
        f"Reply with a single valid JSON object and nothing else. "
        f"No markdown fences, no commentary."
    )


def _with_stricter_instruction(
    messages: list[ChatMessage], previous_error: str
) -> list[ChatMessage]:
    """Append a firmer instruction to a conversation after a parse failure."""
    return [
        *messages,
        {
            "role": "user",
            "content": (
                f"Your previous reply could not be parsed ({previous_error}). "
                f"Either call one tool, or reply with a single valid JSON object "
                f"and nothing else. No markdown fences, no commentary."
            ),
        },
    ]


@dataclass
class StubLanguageModel:
    """Deterministic stand-in for tests and offline runs.

    Two behaviours, both offline:

    - With no script it answers immediately with a fixed, schema-valid
      object, so the loop, the job queue and the persistence path can all be
      exercised with no model installed.
    - With `scripted_turns` it drives the reason-act loop step by step, which
      is how a test asserts that *the model's* tool choice is honoured
      without a non-deterministic model in the assertion (rule R3).
    """

    name: str = "stub-model"
    canned_response: dict[str, Any] = field(default_factory=dict)
    prompts_seen: list[tuple[str, str]] = field(default_factory=list)
    scripted_turns: list[ModelTurn] = field(default_factory=list)
    tool_manifests_seen: list[list[ToolSchema]] = field(default_factory=list)
    turns_taken: int = 0

    @property
    def model_name(self) -> str:
        """The stub's identifier."""
        return self.name

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        """Record the prompts and return the canned response."""
        self.prompts_seen.append((system_prompt, user_prompt))
        return self._answer()

    def complete_with_tools(
        self, messages: list[ChatMessage], tools: list[ToolSchema]
    ) -> ModelTurn:
        """Record the conversation and play the next scripted turn.

        Args:
            messages: The conversation so far.
            tools: The manifest offered, recorded so a test can assert what
                the model was actually told it could call.

        Returns:
            The next scripted turn, or a final answer once the script is
            exhausted - so an unscripted stub behaves exactly as it did
            before tool calling existed.
        """
        self.prompts_seen.append(split_conversation(messages))
        self.tool_manifests_seen.append(list(tools))

        turn_index = self.turns_taken
        self.turns_taken += 1
        if turn_index < len(self.scripted_turns):
            return self.scripted_turns[turn_index]
        return ModelTurn(final_content=self._answer())

    def _answer(self) -> dict[str, Any]:
        """The canned answer, or a schema-valid default."""
        if self.canned_response:
            return dict(self.canned_response)
        return {
            "reasons": ["Deterministic stub explanation."],
            "concerns": [],
            "missing_information": [],
            "citations": [],
        }
