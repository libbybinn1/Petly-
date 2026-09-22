"""Language-model access for the agent.

The model's only jobs are interpreting natural-language intent and writing
explanations. It never produces a score - that is deterministic Python, per
spec section 8 and docs/AGENT.md section 1.

Wrapped behind a small protocol so the agent depends on the capability rather
than on Ollama, and so tests can substitute a deterministic stand-in and run
with no model installed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

# A model occasionally emits prose around its JSON, or truncates it. Retrying
# with a firmer instruction usually fixes it; retrying forever would hang the
# queue, so the attempts are bounded.
MAX_JSON_ATTEMPTS = 3


class LanguageModelUnavailableError(RuntimeError):
    """Raised when the model cannot be reached at all."""


class MalformedModelOutputError(ValueError):
    """Raised when the model never produced parseable JSON."""


class LanguageModel(Protocol):
    """Anything that can answer a prompt with JSON."""

    @property
    def model_name(self) -> str:
        """Identifier recorded on each analysis for auditability."""
        ...

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        """Return a parsed JSON object for the given prompts."""
        ...


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

        Raises:
            LanguageModelUnavailableError: The model host is unreachable.
            MalformedModelOutputError: No attempt produced valid JSON.
        """
        import ollama

        client = ollama.Client(host=self.base_url)
        last_error: str = ""

        for attempt in range(1, MAX_JSON_ATTEMPTS + 1):
            prompt = user_prompt if attempt == 1 else _stricter(user_prompt, last_error)
            try:
                response = client.chat(
                    model=self.name,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    format="json",
                    options={"temperature": self.temperature},
                )
            except Exception as error:  # - surfaced as a typed failure
                raise LanguageModelUnavailableError(
                    f"Could not reach {self.name} at {self.base_url}: {error}"
                ) from error

            raw_text = response["message"]["content"]
            try:
                parsed = json.loads(raw_text)
            except json.JSONDecodeError as error:
                last_error = str(error)
                continue

            if isinstance(parsed, dict):
                return parsed
            last_error = f"expected a JSON object, got {type(parsed).__name__}"

        raise MalformedModelOutputError(
            f"{self.name} produced no valid JSON object in {MAX_JSON_ATTEMPTS} attempts: "
            f"{last_error}"
        )


def _stricter(original_prompt: str, previous_error: str) -> str:
    """Re-ask more firmly after an unparseable reply."""
    return (
        f"{original_prompt}\n\n"
        f"Your previous reply could not be parsed ({previous_error}). "
        f"Reply with a single valid JSON object and nothing else. "
        f"No markdown fences, no commentary."
    )


@dataclass
class StubLanguageModel:
    """Deterministic stand-in for tests and offline runs.

    Returns a fixed, schema-valid response so the agent loop, the job queue
    and the persistence path can all be exercised with no model installed.
    """

    name: str = "stub-model"
    canned_response: dict[str, Any] = field(default_factory=dict)
    prompts_seen: list[tuple[str, str]] = field(default_factory=list)

    @property
    def model_name(self) -> str:
        """The stub's identifier."""
        return self.name

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        """Record the prompts and return the canned response."""
        self.prompts_seen.append((system_prompt, user_prompt))
        if self.canned_response:
            return dict(self.canned_response)
        return {
            "reasons": ["Deterministic stub explanation."],
            "concerns": [],
            "missing_information": [],
        }
