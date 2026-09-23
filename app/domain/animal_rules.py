"""Validation rules for the animal records staff create and edit (spec 5.2).

Pure functions over plain values, with no Flask and no SQLAlchemy, so the
rules are testable without a request or a database (rule R2).

Two properties are deliberate and shared with `profile_rules`:

- **Every problem is reported at once.** Returning the first error would
  make somebody with four mistakes reload four times to find them.
- **The server-side check is the real one.** A browser's `required`
  attribute is a convenience that a forged POST ignores entirely (NFR-5.2),
  and the enum CHECK constraints on `animals` will reject a bad value with a
  database error rather than a message a person can read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TypeVar

from app.domain.enums import ActivityLevel, AnimalSize, AnimalStatus, Species, Temperament

# Matched to the column widths in docs/MODEL_DATA.md section 2.3. SQLite
# stores an oversized string happily and SQL Server 2014 refuses it, so a
# missing maximum here fails only in front of a real user.
MAXIMUM_NAME_LENGTH = 100
MAXIMUM_BREED_LENGTH = 100
MAXIMUM_CITY_LENGTH = 100
MAXIMUM_SPECIAL_NEEDS_LENGTH = 1000
MAXIMUM_DESCRIPTION_LENGTH = 4000

# Older than any animal this shelter rehomes, and old enough that a typo
# such as 250 is caught rather than stored.
MAXIMUM_AGE_YEARS = 40.0

EnumT = TypeVar("EnumT", bound=Enum)


class NoImageError(ValueError):
    """Raised when an animal would be saved without a photograph."""


@dataclass(frozen=True)
class AnimalSubmission:
    """The raw strings an animal form produces.

    Every field is optional and untyped on purpose: this is what arrived,
    not what is valid. Validation turns it into `AnimalFactsDraft`.
    """

    name: str | None = None
    species: str | None = None
    breed: str | None = None
    age_years: str | None = None
    size: str | None = None
    temperament: str | None = None
    activity_level: str | None = None
    required_space: str | None = None
    city: str | None = None
    status: str | None = None
    description: str | None = None
    good_with_children: bool = False
    good_with_other_animals: bool = False
    has_special_needs: bool = False
    special_needs_description: str | None = None
    image_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class AnimalFactsDraft:
    """A validated animal, ready to be written."""

    name: str
    species: Species
    breed: str | None
    age_years: float
    size: AnimalSize
    temperament: Temperament
    activity_level: ActivityLevel
    required_space: AnimalSize
    city: str
    status: AnimalStatus
    description: str | None
    good_with_children: bool
    good_with_other_animals: bool
    has_special_needs: bool
    special_needs_description: str | None
    image_urls: tuple[str, ...]


@dataclass(frozen=True)
class AnimalValidationResult:
    """The outcome of validating one submission."""

    errors: dict[str, str] = field(default_factory=dict)
    animal: AnimalFactsDraft | None = None

    @property
    def is_valid(self) -> bool:
        """Whether the submission produced a usable animal."""
        return not self.errors and self.animal is not None


def validate_animal(submission: AnimalSubmission) -> AnimalValidationResult:
    """Check one animal submission, collecting every problem.

    Args:
        submission: The raw form values.

    Returns:
        A result carrying either the validated animal or a field-keyed map
        of messages written for the person who filled the form in.
    """
    errors: dict[str, str] = {}

    name = _parse_required_text(
        submission.name, "name", "a name", MAXIMUM_NAME_LENGTH, errors
    )
    city = _parse_required_text(
        submission.city, "city", "a city", MAXIMUM_CITY_LENGTH, errors
    )
    species = _parse_required_enum(Species, submission.species, "species", errors)
    size = _parse_required_enum(AnimalSize, submission.size, "size", errors)
    temperament = _parse_required_enum(
        Temperament, submission.temperament, "temperament", errors
    )
    activity = _parse_required_enum(
        ActivityLevel, submission.activity_level, "activity_level", errors
    )
    required_space = _parse_required_enum(
        AnimalSize, submission.required_space, "required_space", errors
    )
    status = _parse_status(submission.status, errors)
    age_years = _parse_age(submission.age_years, errors)
    breed = _parse_optional_text(submission.breed, "breed", MAXIMUM_BREED_LENGTH, errors)
    description = _parse_optional_text(
        submission.description, "description", MAXIMUM_DESCRIPTION_LENGTH, errors
    )
    special_needs = _parse_special_needs(submission, errors)
    image_urls = _parse_images(submission.image_urls, errors)

    if errors:
        return AnimalValidationResult(errors=errors)

    assert species is not None
    assert size is not None
    assert temperament is not None
    assert activity is not None
    assert required_space is not None
    assert status is not None
    assert name is not None
    assert city is not None
    assert age_years is not None

    return AnimalValidationResult(
        animal=AnimalFactsDraft(
            name=name,
            species=species,
            breed=breed,
            age_years=age_years,
            size=size,
            temperament=temperament,
            activity_level=activity,
            required_space=required_space,
            city=city,
            status=status,
            description=description,
            good_with_children=submission.good_with_children,
            good_with_other_animals=submission.good_with_other_animals,
            has_special_needs=submission.has_special_needs,
            special_needs_description=special_needs,
            image_urls=image_urls,
        )
    )


def ensure_animal_has_an_image(image_urls: tuple[str, ...]) -> None:
    """Raise unless the animal has at least one photograph (FR-4.2).

    Spec 24 makes this mandatory, and it cannot be a table constraint: it is
    a cross-table cardinality rule, so nothing in the schema can enforce it.
    It lives here instead, and is checked on every write rather than only on
    creation - an edit that removed the last photo would otherwise slip past
    a rule the create path enforced.

    Args:
        image_urls: The photo URLs the animal will have after the write.

    Raises:
        NoImageError: The animal would have no photograph at all.
    """
    if not image_urls:
        raise NoImageError(
            "Every animal needs at least one photograph before it can be listed."
        )


def next_display_order(existing_count: int) -> int:
    """The display order a newly added photo takes."""
    return existing_count


def _parse_required_text(
    raw_value: str | None,
    field_name: str,
    description: str,
    maximum_length: int,
    errors: dict[str, str],
) -> str | None:
    """Parse a required free-text field, recording any problem."""
    text = (raw_value or "").strip()
    if not text:
        errors[field_name] = f"Please give this animal {description}."
        return None
    if len(text) > maximum_length:
        errors[field_name] = f"Please keep this under {maximum_length} characters."
        return None
    return text


def _parse_optional_text(
    raw_value: str | None,
    field_name: str,
    maximum_length: int,
    errors: dict[str, str],
) -> str | None:
    """Parse an optional free-text field, recording only a length problem."""
    text = (raw_value or "").strip()
    if not text:
        return None
    if len(text) > maximum_length:
        errors[field_name] = f"Please keep this under {maximum_length} characters."
        return None
    return text


def _parse_required_enum(
    enum_class: type[EnumT],
    raw_value: str | None,
    field_name: str,
    errors: dict[str, str],
) -> EnumT | None:
    """Parse a required enum field, rejecting anything not in the vocabulary."""
    if not raw_value:
        errors[field_name] = "Please choose an option."
        return None
    try:
        return enum_class(raw_value.strip().upper())
    except ValueError:
        errors[field_name] = "That is not one of the available options."
        return None


def _parse_status(raw_value: str | None, errors: dict[str, str]) -> AnimalStatus | None:
    """Parse the status, defaulting a blank to AVAILABLE.

    A new animal is being listed, so AVAILABLE is the only sensible default
    and asking staff to pick it every time would be noise. An explicit bad
    value is still an error - that is a forged post, not a blank field.
    """
    if not raw_value:
        return AnimalStatus.AVAILABLE
    return _parse_required_enum(AnimalStatus, raw_value, "status", errors)


def _parse_age(raw_value: str | None, errors: dict[str, str]) -> float | None:
    """Parse the age in years, rejecting anything impossible."""
    if raw_value is None or not str(raw_value).strip():
        errors["age_years"] = "Please give an approximate age in years."
        return None
    try:
        age = float(str(raw_value).strip())
    except ValueError:
        errors["age_years"] = "Please give the age as a number, for example 2.5."
        return None

    if age < 0:
        errors["age_years"] = "An age cannot be negative."
        return None
    if age > MAXIMUM_AGE_YEARS:
        errors["age_years"] = f"Please give an age under {MAXIMUM_AGE_YEARS:.0f} years."
        return None
    return age


def _parse_special_needs(
    submission: AnimalSubmission, errors: dict[str, str]
) -> str | None:
    """Parse the special-needs note, keeping it consistent with the flag.

    A description without the flag is dropped rather than stored, so the
    database never holds a note about needs the record says do not exist.
    A flag without a description is an error: "has special needs" with no
    explanation tells an adopter nothing and disqualifies nothing usefully.
    """
    text = (submission.special_needs_description or "").strip()

    if not submission.has_special_needs:
        return None

    if not text:
        errors["special_needs_description"] = (
            "Please describe the special needs so adopters know what is involved."
        )
        return None

    if len(text) > MAXIMUM_SPECIAL_NEEDS_LENGTH:
        errors["special_needs_description"] = (
            f"Please keep this under {MAXIMUM_SPECIAL_NEEDS_LENGTH} characters."
        )
        return None

    return text


def _parse_images(raw_urls: tuple[str, ...], errors: dict[str, str]) -> tuple[str, ...]:
    """Parse the photo URLs, requiring at least one (FR-4.2)."""
    cleaned = tuple(dict.fromkeys(url.strip() for url in raw_urls if url.strip()))
    if not cleaned:
        errors["image_urls"] = (
            "Please add at least one photograph. An animal cannot be listed without one."
        )
    return cleaned
