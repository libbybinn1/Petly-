"""Turning an adopter's own words into structured search criteria (spec 6.3).

The adopter writes something like *"I feel like getting something like a
hamster or rabbit, a small rodent"*. This module asks the language model to
convert that into the fields the deterministic search understands.

The vocabulary of the answer, `SearchIntent`, and its JSON form on the job
row live in the domain (`app.domain.search_intent`) because both processes
speak it: the agent writes it, the web tier reads it. This module owns only
the part that needs a model, and re-exports the contract names so callers
inside the agent have one import.

**The model interprets; it does not choose.** It produces criteria, and the
ordinary search then runs against them. Nothing the model returns selects an
animal or produces a score.
"""

from __future__ import annotations

from pathlib import Path

from app.domain.search_intent import (
    MAX_INTERPRETATION_LENGTH,
    MAX_SPECIES_SELECTIONS,
    SearchIntent,
    intent_from_mapping,
    intent_to_payload,
    payload_to_intent,
)

from agent_service.llm_client import (
    JsonCompletion,
    LanguageModelUnavailableError,
    MalformedModelOutputError,
)

__all__ = [
    "MAX_INTERPRETATION_LENGTH",
    "MAX_SPECIES_SELECTIONS",
    "IntentInterpreter",
    "SearchIntent",
    "intent_from_mapping",
    "intent_to_payload",
    "payload_to_intent",
]

PROMPT_DIRECTORY = Path(__file__).resolve().parent / "prompts"


class IntentInterpreter:
    """Converts free text into search criteria using a language model."""

    def __init__(self, language_model: JsonCompletion) -> None:
        """Bind the interpreter to a model.

        Args:
            language_model: Anything that answers a prompt with JSON. The
                interpreter never uses tools, so it asks for no more than
                that.
        """
        self._language_model = language_model

    def interpret(self, free_text: str) -> SearchIntent:
        """Extract structured criteria from an adopter's description.

        Args:
            free_text: What the adopter typed.

        Returns:
            The extracted criteria. When the model is unavailable or the text
            is unusable, an intent marked not-understood, so the caller can
            fall back to ordinary search rather than failing.
        """
        if not free_text.strip():
            return SearchIntent.not_understood("Please describe what you are looking for.")

        system_prompt = (PROMPT_DIRECTORY / "intent_system.md").read_text(encoding="utf-8")

        try:
            raw = self._language_model.complete_json(system_prompt, free_text.strip())
        except LanguageModelUnavailableError:
            return SearchIntent.not_understood(
                "We could not interpret that just now. Try the filters instead."
            )
        except MalformedModelOutputError:
            return SearchIntent.not_understood(
                "We could not make sense of that. Try rephrasing, or use the filters."
            )

        # The model's reply is validated by the same rule that validates a
        # stored payload: every value checked against its enum, unknown
        # values dropped, so an invented species cannot become a filter.
        return intent_from_mapping(raw)
