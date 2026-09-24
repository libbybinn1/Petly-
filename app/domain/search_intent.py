"""The natural-language search intent, and its JSON form (spec 6.3, 6.4).

An adopter's free text is interpreted by the agent process into structured
criteria, which travel back to the web tier as JSON on the analysis job row.
Both processes therefore need the same vocabulary, and shared vocabulary
lives in the domain: this module has no framework and no model dependency,
so the web tier can read an intent without loading any agent code, and the
agent can write one without importing the web tier (rule R2).

Two boundaries hold on both sides of the contract.

**The model interprets; it does not choose.** An intent holds criteria only.
The ordinary deterministic search runs against them; nothing here selects
an animal or produces a score.

**Absence is preserved.** A criterion the adopter did not express stays
`None`, meaning "no constraint". Guessing a value would silently narrow the
results with no way for the adopter to see why.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TypeVar

from app.domain.enums import ActivityLevel, AnimalSize, Species, Temperament

MAX_INTERPRETATION_LENGTH = 200
MAX_SPECIES_SELECTIONS = 4

EnumT = TypeVar("EnumT", bound=Enum)


@dataclass(frozen=True)
class SearchIntent:
    """Structured criteria extracted from free text (spec 6.3)."""

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


def intent_to_payload(intent: SearchIntent) -> dict[str, object]:
    """Render an intent as the JSON an `INTERPRET_INTENT` job stores.

    The agent runs in its own process, so an interpreted intent reaches the
    web tier as JSON on the job row rather than as an object. This function
    and `payload_to_intent` are that contract, kept together so it cannot
    drift: enum members become their string values, and a criterion the
    adopter did not express stays null, meaning "no constraint".

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


def intent_from_mapping(raw: Mapping[str, object]) -> SearchIntent:
    """Validate a raw mapping, from the model or from storage, into an intent.

    Every value is checked against its enum rather than trusted. A model
    inventing a species, or a stored value from an older enum, would
    otherwise become a filter that silently matches nothing. Anything
    unrecognised is dropped, so the worst case is a broader search rather
    than a wrong one.

    Args:
        raw: The model's JSON reply, or a decoded `result_payload`.

    Returns:
        The validated intent, or a not-understood intent when the mapping
        says the text could not be interpreted.
    """
    if not raw.get("understood"):
        return SearchIntent.not_understood(
            str(raw.get("interpretation") or "We could not interpret that request.")
        )

    interpretation = str(raw.get("interpretation") or "").strip()
    return SearchIntent(
        understood=True,
        species=parse_species_list(raw.get("species")),
        size=parse_enum(AnimalSize, raw.get("size")),
        activity_level=parse_enum(ActivityLevel, raw.get("activity_level")),
        temperament=parse_enum(Temperament, raw.get("temperament")),
        # Only True is meaningful. False would mean the adopter actively
        # wants an animal that is bad with children, which nobody means.
        good_with_children=True if raw.get("good_with_children") is True else None,
        good_with_other_animals=(
            True if raw.get("good_with_other_animals") is True else None
        ),
        interpretation=interpretation[:MAX_INTERPRETATION_LENGTH],
    )


def payload_to_intent(payload: Mapping[str, object]) -> SearchIntent:
    """Read back an intent stored by an `INTERPRET_INTENT` job.

    The stored payload is data the web tier did not write, so it is checked
    rather than trusted, by the same rule that applies to the model's reply.

    Args:
        payload: The decoded `result_payload` of a completed job.

    Returns:
        The intent, with anything unrecognised dropped.
    """
    return intent_from_mapping(payload)


def parse_species_list(raw_value: object) -> tuple[Species, ...]:
    """Parse the species list, discarding anything unrecognised.

    Args:
        raw_value: Whatever the mapping held, which may not be a list.

    Returns:
        Up to `MAX_SPECIES_SELECTIONS` distinct species, in the order given.
    """
    if not isinstance(raw_value, list):
        return ()

    parsed: list[Species] = []
    for item in raw_value[:MAX_SPECIES_SELECTIONS]:
        try:
            parsed.append(Species(str(item).strip().upper()))
        except ValueError:
            continue
    return tuple(dict.fromkeys(parsed))


def parse_enum(enum_class: type[EnumT], raw_value: object) -> EnumT | None:
    """Parse one optional enum value, returning None when absent or unknown.

    Generic over the enum so each caller keeps its own precise type: the
    size field reads back as an `AnimalSize`, not as a bare `Enum`.

    Args:
        enum_class: The enum the value must belong to.
        raw_value: Whatever the mapping held for the field.

    Returns:
        The matching member, or None when the field was absent or the value
        is one the enum does not define.
    """
    if raw_value is None:
        return None
    try:
        return enum_class(str(raw_value).strip().upper())
    except ValueError:
        return None
