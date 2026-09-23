"""Turning an adopter's own words into structured search criteria (spec 6.3).

The adopter writes something like *"I feel like getting something like a
hamster or rabbit, a small rodent"*. This module converts that into the
fields the deterministic search understands.

Two boundaries matter here.

**The model interprets; it does not choose.** It produces criteria, and the
ordinary search then runs against them. Nothing the model returns selects an
animal or produces a score.

**Absence is preserved.** A criterion the adopter did not express stays
`None`, meaning "no constraint". Guessing a value would silently narrow the
results with no way for the adopter to see why, which is worse than a broad
result set.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TypeVar

from app.domain.enums import ActivityLevel, AnimalSize, Species, Temperament

from agent_service.llm_client import (
    JsonCompletion,
    LanguageModelUnavailableError,
    MalformedModelOutputError,
)

PROMPT_DIRECTORY = Path(__file__).resolve().parent / "prompts"

MAX_INTERPRETATION_LENGTH = 200
MAX_SPECIES_SELECTIONS = 4


@dataclass(frozen=True)
class SearchIntent:
    """Structured criteria extracted from free text."""

    understood: bool
    species: tuple[Species, ...] = ()
    size: AnimalSize | None = None
    activity_level: ActivityLevel | None = None
    temperament: Temperament | None = None
    good_with_children: bool | None = None
    good_with_other_animals: bool | None = None
    interpretation: str = ""

    @property
    def has_any_criteria(self) -> bool:
        """Whether anything at all was extracted."""
        return bool(
            self.species
            or self.size
            or self.activity_level
            or self.temperament
            or self.good_with_children
            or self.good_with_other_animals
        )

    @classmethod
    def not_understood(cls, reason: str) -> SearchIntent:
        """Build the result for text that could not be interpreted.

        Returned rather than raised: a request the model could not parse is
        an ordinary outcome that the interface should explain, not an error.
        """
        return cls(understood=False, interpretation=reason)


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

        return _build_intent(raw)


def intent_to_payload(intent: SearchIntent) -> dict[str, object]:
    """Render an intent as the JSON an `INTERPRET_INTENT` job stores.

    The agent runs in its own process, so an interpreted intent reaches the
    web tier as JSON on the job row rather than as an object (spec section
    6.3). These two functions are that contract, kept together so it cannot
    drift apart: enum members become their string values, and a criterion
    the adopter did not express stays null, meaning "no constraint".

    Args:
        intent: What the interpreter extracted.

    Returns:
        A JSON-serialisable mapping with one key per `SearchIntent` field.
    """
    return {
        "understood": intent.understood,
        "species": [species.value for species in intent.species],
        "size": intent.size.value if intent.size is not None else None,
        "activity_level": (
            intent.activity_level.value if intent.activity_level is not None else None
        ),
        "temperament": intent.temperament.value if intent.temperament is not None else None,
        "good_with_children": intent.good_with_children,
        "good_with_other_animals": intent.good_with_other_animals,
        "interpretation": intent.interpretation,
    }


def payload_to_intent(payload: dict[str, object]) -> SearchIntent:
    """Read back an intent stored by an `INTERPRET_INTENT` job.

    Every value is validated against its enum on the way back in, exactly as
    it is when it first arrives from the model. The stored payload is data
    the web tier did not write, so it is checked rather than trusted - which
    also means a stored value from an older enum cannot become a filter that
    silently matches nothing.

    Args:
        payload: The decoded `result_payload` of a completed job.

    Returns:
        The intent. Anything unrecognised is dropped, so the worst case is a
        broader search rather than a wrong one.
    """
    if not payload.get("understood"):
        return SearchIntent.not_understood(
            str(payload.get("interpretation") or "We could not interpret that request.")
        )

    return SearchIntent(
        understood=True,
        species=_parse_species_list(payload.get("species")),
        size=_parse_enum(AnimalSize, payload.get("size")),
        activity_level=_parse_enum(ActivityLevel, payload.get("activity_level")),
        temperament=_parse_enum(Temperament, payload.get("temperament")),
        # Only True is meaningful, the same rule as when the model answered.
        good_with_children=True if payload.get("good_with_children") is True else None,
        good_with_other_animals=(
            True if payload.get("good_with_other_animals") is True else None
        ),
        interpretation=str(payload.get("interpretation") or "")[:MAX_INTERPRETATION_LENGTH],
    )


def _build_intent(raw: dict[str, object]) -> SearchIntent:
    """Convert the model's JSON into a validated intent.

    Every value is checked against the enums rather than trusted. A model
    inventing a species would otherwise produce a filter that silently
    matches nothing.
    """
    if not raw.get("understood"):
        return SearchIntent.not_understood(
            str(raw.get("interpretation") or "We could not interpret that request.")
        )

    species = _parse_species_list(raw.get("species"))
    interpretation = str(raw.get("interpretation") or "").strip()

    return SearchIntent(
        understood=True,
        species=species,
        size=_parse_enum(AnimalSize, raw.get("size")),
        activity_level=_parse_enum(ActivityLevel, raw.get("activity_level")),
        temperament=_parse_enum(Temperament, raw.get("temperament")),
        # Only True is meaningful. False would mean the adopter actively
        # wants an animal that is bad with children, which nobody means.
        good_with_children=True if raw.get("good_with_children") is True else None,
        good_with_other_animals=(
            True if raw.get("good_with_other_animals") is True else None
        ),
        interpretation=interpretation[:MAX_INTERPRETATION_LENGTH],
    )


def _parse_species_list(raw_value: object) -> tuple[Species, ...]:
    """Parse the species list, discarding anything unrecognised."""
    if not isinstance(raw_value, list):
        return ()

    parsed: list[Species] = []
    for item in raw_value[:MAX_SPECIES_SELECTIONS]:
        try:
            parsed.append(Species(str(item).strip().upper()))
        except ValueError:
            continue
    return tuple(dict.fromkeys(parsed))


EnumT = TypeVar("EnumT", bound=Enum)


def _parse_enum(enum_class: type[EnumT], raw_value: object) -> EnumT | None:
    """Parse one optional enum value, returning None when absent or unknown.

    Generic over the enum so each caller keeps its own precise type: the
    size field reads back as an `AnimalSize`, not as a bare `Enum`.

    Args:
        enum_class: The enum the value must belong to.
        raw_value: Whatever the model returned for the field.

    Returns:
        The matching member, or None when the field was absent or the
        model invented a value the enum does not define.
    """
    if raw_value is None:
        return None
    try:
        return enum_class(str(raw_value).strip().upper())
    except ValueError:
        return None
