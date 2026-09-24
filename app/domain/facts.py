"""The one place a stored or transported record becomes matching facts.

Spec section 8 requires that the same pairing always produces the same score.
That guarantee is only as good as the conversion in front of the scorer, and
this project had three of them: the web tier read ORM rows, the agent read MCP
payloads, and the history seeder read rows again. They drifted. Two of the
three never set `preferred_age_range` or `preferred_size`, so the agent
recomputed 95 where the web tier had shown 91, and wrote "the adopter
expressed no age preference" about an adopter who had expressed one - which
rule R4 forbids outright, because it is a claim traceable to nothing.

The fix is structural rather than a third copy kept in step by hand: the
field names are declared once, the parsing rules are declared once, and both
tiers call the same function. The two sources differ in exactly one way - a
row stores `preferred_species` as a comma-separated string because SQL Server
2014 has no array type, while a payload carries a JSON list - so the species
parser accepts either and nothing else has to know which side it is on.

Every rule here fails *towards the safe answer* rather than raising: a single
unreadable value in one record must degrade that record's score, never stop
the queue or the page (FR-10.10).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from enum import Enum
from typing import TypeVar

from app.domain.enums import (
    ActivityLevel,
    AnimalSize,
    ExperienceLevel,
    HomeType,
    Species,
    Temperament,
)
from app.domain.matching import AdopterFacts, AgePreference, AnimalFacts

logger = logging.getLogger("petmatch.domain.facts")

EnumMember = TypeVar("EnumMember", bound=Enum)

# The record fields each fact object is built from. Declared once so a row and
# a payload cannot disagree about which fields matter, and so reading them off
# an ORM object needs no second list to fall out of step with this one.
ADOPTER_FACT_FIELDS: tuple[str, ...] = (
    "home_type",
    "has_yard",
    "household_has_children",
    "youngest_child_age",
    "has_other_animals",
    "experience_level",
    "activity_level",
    "daily_hours_available",
    "city",
    "preferred_species",
    "preferred_age_range",
    "preferred_size",
    "open_to_proactive_suggestions",
    "is_complete",
)

ANIMAL_FACT_FIELDS: tuple[str, ...] = (
    "species",
    "age_years",
    "size",
    "temperament",
    "activity_level",
    "good_with_children",
    "good_with_other_animals",
    "has_special_needs",
    "required_space",
    "city",
)


def adopter_facts_from(record: Mapping[str, object]) -> AdopterFacts:
    """Convert one adopter record into the facts the scorer reads (spec section 8).

    Args:
        record: The adopter's fields, keyed by the column names in
            `ADOPTER_FACT_FIELDS`. An absent key is read as "not stated".

    Returns:
        The domain value object. Unrecognised enum values fall back to the
        safe default for that field, except the two *preferences*, which
        degrade to "none expressed" rather than to an invented choice.
    """
    return AdopterFacts(
        home_type=parse_enum(HomeType, record.get("home_type"), HomeType.APARTMENT),
        has_yard=bool(record.get("has_yard")),
        household_has_children=bool(record.get("household_has_children")),
        youngest_child_age=parse_optional_whole_number(record.get("youngest_child_age")),
        has_other_animals=bool(record.get("has_other_animals")),
        experience_level=parse_enum(
            ExperienceLevel, record.get("experience_level"), ExperienceLevel.NONE
        ),
        activity_level=parse_enum(
            ActivityLevel, record.get("activity_level"), ActivityLevel.MODERATE
        ),
        daily_hours_available=parse_number(record.get("daily_hours_available")),
        city=parse_text(record.get("city")),
        preferred_species=parse_species_preferences(record.get("preferred_species")),
        preferred_age_range=parse_optional_enum(
            AgePreference, record.get("preferred_age_range")
        ),
        preferred_size=parse_optional_enum(AnimalSize, record.get("preferred_size")),
        open_to_proactive_suggestions=bool(record.get("open_to_proactive_suggestions")),
        is_complete=bool(record.get("is_complete")),
    )


def animal_facts_from(record: Mapping[str, object]) -> AnimalFacts:
    """Convert one animal record into the facts the scorer reads (spec section 8).

    Args:
        record: The animal's fields, keyed by the column names in
            `ANIMAL_FACT_FIELDS`. An absent key is read as "not stated".

    Returns:
        The domain value object. An unreadable species is reported as unknown
        rather than silently classified, so the species criterion can decline
        to judge it - see `parse_species`.
    """
    species, species_is_known = parse_species(record.get("species"))
    return AnimalFacts(
        species=species,
        age_years=parse_number(record.get("age_years")),
        size=parse_enum(AnimalSize, record.get("size"), AnimalSize.MEDIUM),
        temperament=parse_enum(
            Temperament, record.get("temperament"), Temperament.BALANCED
        ),
        activity_level=parse_enum(
            ActivityLevel, record.get("activity_level"), ActivityLevel.MODERATE
        ),
        good_with_children=bool(record.get("good_with_children")),
        good_with_other_animals=bool(record.get("good_with_other_animals")),
        has_special_needs=bool(record.get("has_special_needs")),
        required_space=parse_enum(
            AnimalSize, record.get("required_space"), AnimalSize.MEDIUM
        ),
        city=parse_text(record.get("city")),
        species_is_known=species_is_known,
    )


def adopter_facts_from_row(row: object) -> AdopterFacts:
    """Convert a stored adopter row into matching facts (spec section 8).

    Args:
        row: Anything exposing the `ADOPTER_FACT_FIELDS` as attributes - in
            practice an `AdopterProfile` ORM instance. The domain never
            imports the ORM (rule R2), so the row is read by name.

    Returns:
        The same facts `adopter_facts_from` would build from the same values.
    """
    return adopter_facts_from(fields_of(row, ADOPTER_FACT_FIELDS))


def animal_facts_from_row(row: object) -> AnimalFacts:
    """Convert a stored animal row into matching facts (spec section 8).

    Args:
        row: Anything exposing the `ANIMAL_FACT_FIELDS` as attributes - in
            practice an `Animal` ORM instance.

    Returns:
        The same facts `animal_facts_from` would build from the same values.
    """
    return animal_facts_from(fields_of(row, ANIMAL_FACT_FIELDS))


def fields_of(row: object, field_names: Iterable[str]) -> dict[str, object]:
    """Read named attributes off a record into a plain mapping.

    Args:
        row: The record to read. An attribute it does not carry is read as
            None, which every parser below treats as "not stated".
        field_names: Which attributes to read.

    Returns:
        The values, keyed by field name.
    """
    return {name: getattr(row, name, None) for name in field_names}


def parse_enum(
    enum_class: type[EnumMember], raw_value: object, default: EnumMember
) -> EnumMember:
    """Read a stored string as an enum member, falling back on a bad value.

    Generic over the enum so a decoded `home_type` is typed as a `HomeType`
    rather than as a bare object, which is what lets the type checker verify
    that the fact objects are assembled from the right enums.

    Args:
        enum_class: The enum the stored string should name.
        raw_value: The stored value, of whatever type arrived.
        default: Used when the value names no member.

    Returns:
        The matching member, or the default.
    """
    if raw_value is None:
        return default
    try:
        return enum_class(str(raw_value))
    except ValueError:
        return default


def parse_optional_enum(
    enum_class: type[EnumMember], raw_value: object
) -> EnumMember | None:
    """Read one optional stored preference, dropping anything unrecognised.

    The same reasoning as `parse_species_preferences`: a value the enum no
    longer defines is unknown, not a preference, and inventing one from it
    would quietly narrow somebody's matches.

    Args:
        enum_class: The enum the stored string should name.
        raw_value: The stored value, which may be absent.

    Returns:
        The matching member, or None when nothing was stated or the value
        names no member.
    """
    if raw_value is None:
        return None
    text = str(raw_value).strip()
    if not text:
        return None
    try:
        return enum_class(text)
    except ValueError:
        return None


def parse_species(raw_value: object) -> tuple[Species, bool]:
    """Read an animal's own species, saying whether it was readable.

    An animal has to carry *some* species for its facts to be assembled, so
    an unreadable value cannot simply be dropped the way an adopter's
    preference can. Recording that it was unreadable is the next best thing:
    `Species.OTHER` is a real catalogue category ("other / exotic"), and
    quietly filing a corrupt value under it would let an adopter who asked
    for exotic animals score 100 on the heaviest criterion against an animal
    nobody classified. `score_species_preference` reads the flag and declines
    to judge instead.

    Args:
        raw_value: The stored species value.

    Returns:
        The species to score with, and whether it was actually readable.
    """
    if raw_value is None:
        return Species.OTHER, False
    try:
        return Species(str(raw_value)), True
    except ValueError:
        logger.warning("unrecognised animal species %r; scored as unknown", raw_value)
        return Species.OTHER, False


def parse_species_preferences(raw_value: object) -> frozenset[Species]:
    """Read an adopter's species preferences, dropping anything unrecognised.

    An unrecognised value must not become `Species.OTHER`. OTHER is a real
    preference, not a marker for "unknown", and species preference carries the
    heaviest weight when ranking animals for an adopter - so a typo or a
    retired value in this column would record the adopter as actively wanting
    animals of no listed species, and score every such animal 100 on that
    criterion. Dropping the value records what is actually known about it,
    which is nothing.

    Values are case-sensitive, matching the CHECK constraint the column
    carries: "dog" is not a species this system stores.

    Args:
        raw_value: Either the comma-separated column a row holds, or the list
            an MCP payload carries. Anything else reads as no preference.

    Returns:
        The species that parsed. An unparseable one is left out.
    """
    parsed: list[Species] = []
    for value in _preference_values(raw_value):
        try:
            parsed.append(Species(value))
        except ValueError:
            continue
    return frozenset(parsed)


def _preference_values(raw_value: object) -> list[str]:
    """Normalise either storage shape into a list of trimmed strings."""
    if isinstance(raw_value, str):
        return split_stored_values(raw_value)
    if isinstance(raw_value, list | tuple | frozenset | set):
        return [str(item).strip() for item in raw_value if str(item).strip()]
    return []


def split_stored_values(raw_column: str | None) -> list[str]:
    """Split one delimited column into its values, in stored order.

    Multi-valued preferences live in a single NVARCHAR column because SQL
    Server 2014 has no array type (see the `db-management` skill). Order is
    preserved because the profile form re-selects options in it.

    Args:
        raw_column: The stored column, which may be null or empty.

    Returns:
        The non-empty values, trimmed.
    """
    if not raw_column:
        return []
    return [value.strip() for value in raw_column.split(",") if value.strip()]


def parse_optional_whole_number(raw_value: object) -> int | None:
    """Coerce a record value to a whole number, or None when unusable.

    Every other numeric field is coerced; this one was once passed through
    untouched, so a JSON string age reached the scorer and was compared
    against an int - raising TypeError from inside the child-safety rule and
    failing the whole analysis job. A field that cannot be read should degrade
    to "not stated", not crash the queue.

    Args:
        raw_value: The record value, of whatever type arrived.

    Returns:
        The value as an int, or None if absent or not numeric.
    """
    if raw_value is None:
        return None
    try:
        return int(float(str(raw_value)))
    except (TypeError, ValueError):
        return None


def parse_number(raw_value: object, default: float = 0.0) -> float:
    """Coerce a record value to a float, falling back when it is unusable.

    Args:
        raw_value: The record value, of whatever type arrived.
        default: What an absent or unreadable value scores as.

    Returns:
        The value as a float, or the default.
    """
    if raw_value is None:
        return default
    try:
        return float(str(raw_value))
    except (TypeError, ValueError):
        return default


def parse_text(raw_value: object) -> str:
    """Read a text field, turning a missing value into an empty string.

    A null `city` would otherwise reach `score_location`, which compares
    strings, and raise there rather than scoring as "no city recorded".

    Args:
        raw_value: The record value.

    Returns:
        The text, or an empty string.
    """
    if raw_value is None:
        return ""
    return str(raw_value)
