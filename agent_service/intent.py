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
from pathlib import Path

from app.domain.enums import ActivityLevel, AnimalSize, Species, Temperament

from agent_service.llm_client import (
    LanguageModel,
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

    def __init__(self, language_model: LanguageModel) -> None:
        """Bind the interpreter to a model."""
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


def _parse_enum(enum_class: type, raw_value: object):  # noqa: ANN202 - member or None
    """Parse one optional enum value, returning None when absent or unknown."""
    if raw_value is None:
        return None
    try:
        return enum_class(str(raw_value).strip().upper())
    except ValueError:
        return None
