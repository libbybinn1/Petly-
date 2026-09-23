"""Validation rules for the adopter profile form (spec section 5.1).

Pure validation: no framework, no session, no request object. The same rules
run whether input arrives from a browser form, a test, or a future API, which
is what makes "the server rejects everything the browser would have blocked"
(NFR-5.2) true by construction rather than by discipline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TypeVar

from app.domain.enums import ActivityLevel, ExperienceLevel, HomeType, Species

EnumT = TypeVar("EnumT", bound=Enum)

MINIMUM_CHILD_AGE = 0
MAXIMUM_CHILD_AGE = 18
MINIMUM_DAILY_HOURS = 0.0
MAXIMUM_DAILY_HOURS = 24.0
MAXIMUM_YARD_SIZE_SQM = 100_000
MINIMUM_CITY_LENGTH = 2

# Matches adopter_profiles.city, NVARCHAR(100). A value longer than its
# column is accepted by SQLite and rejected by SQL Server 2014 with "String
# or binary data would be truncated" - so without this check the form works
# in the test suite and returns 500 against the real database.
MAXIMUM_CITY_LENGTH = 100
MAXIMUM_DESCRIPTION_LENGTH = 500


@dataclass(frozen=True)
class ProfileSubmission:
    """Raw, untrusted profile input.

    Every field is a string or None, exactly as an HTML form delivers it.
    Parsing happens during validation so that a malformed number is a
    validation error rather than an exception.
    """

    home_type: str | None = None
    has_yard: bool = False
    yard_size_sqm: str | None = None
    household_has_children: bool = False
    youngest_child_age: str | None = None
    has_other_animals: bool = False
    other_animals_description: str | None = None
    experience_level: str | None = None
    activity_level: str | None = None
    daily_hours_available: str | None = None
    city: str | None = None
    preferred_species: tuple[str, ...] = ()
    open_to_proactive_suggestions: bool = False


@dataclass(frozen=True)
class ValidatedProfile:
    """A submission that passed every rule, with values parsed into types."""

    home_type: HomeType
    has_yard: bool
    yard_size_sqm: int | None
    household_has_children: bool
    youngest_child_age: int | None
    has_other_animals: bool
    other_animals_description: str | None
    experience_level: ExperienceLevel
    activity_level: ActivityLevel
    daily_hours_available: float
    city: str
    preferred_species: tuple[Species, ...]
    open_to_proactive_suggestions: bool


@dataclass
class ValidationResult:
    """The outcome of validating a submission.

    Collects *every* problem rather than stopping at the first, so somebody
    filling in a long form fixes it in one pass instead of discovering
    mistakes one reload at a time.
    """

    errors: dict[str, str] = field(default_factory=dict)
    profile: ValidatedProfile | None = None

    @property
    def is_valid(self) -> bool:
        """Whether the submission passed."""
        return not self.errors and self.profile is not None


def validate_profile(submission: ProfileSubmission) -> ValidationResult:
    """Validate a profile submission (spec section 5.1).

    Args:
        submission: Raw form input.

    Returns:
        A result carrying either the parsed profile or a field-keyed map of
        every problem found.
    """
    errors: dict[str, str] = {}

    home_type = _parse_enum(HomeType, submission.home_type, "home_type", errors,
                            "Please choose the kind of home you live in.")
    experience = _parse_enum(ExperienceLevel, submission.experience_level,
                             "experience_level", errors,
                             "Please tell us how much experience you have.")
    activity = _parse_enum(ActivityLevel, submission.activity_level,
                           "activity_level", errors,
                           "Please tell us how active your household is.")

    daily_hours = _parse_daily_hours(submission.daily_hours_available, errors)
    yard_size = _parse_yard_size(submission, errors)
    child_age = _parse_child_age(submission, errors)
    city = _parse_city(submission.city, errors)
    species = _parse_species(submission.preferred_species, errors)
    description = _parse_other_animals_description(submission, errors)

    if errors:
        return ValidationResult(errors=errors)

    assert home_type is not None
    assert experience is not None
    assert activity is not None
    assert daily_hours is not None
    assert city is not None

    return ValidationResult(
        profile=ValidatedProfile(
            home_type=home_type,
            has_yard=submission.has_yard,
            yard_size_sqm=yard_size,
            household_has_children=submission.household_has_children,
            youngest_child_age=child_age,
            has_other_animals=submission.has_other_animals,
            other_animals_description=description,
            experience_level=experience,
            activity_level=activity,
            daily_hours_available=daily_hours,
            city=city,
            preferred_species=species,
            open_to_proactive_suggestions=submission.open_to_proactive_suggestions,
        )
    )


def _parse_enum(
    enum_class: type[EnumT],
    raw_value: str | None,
    field_name: str,
    errors: dict[str, str],
    message: str,
) -> EnumT | None:
    """Parse a required enum field, recording an error when it is absent or unknown.

    Generic over the enum so each call site keeps its precise type: asking
    for a HomeType gets back `HomeType | None`, not an untyped value.
    """
    if not raw_value:
        errors[field_name] = message
        return None
    try:
        return enum_class(raw_value)
    except ValueError:
        errors[field_name] = message
        return None


def _parse_daily_hours(raw_value: str | None, errors: dict[str, str]) -> float | None:
    """Parse the required daily-hours field."""
    if raw_value is None or not str(raw_value).strip():
        errors["daily_hours_available"] = "Please tell us how many hours a day you have."
        return None

    try:
        hours = float(raw_value)
    except ValueError:
        errors["daily_hours_available"] = "Enter a number of hours, for example 2.5."
        return None

    if not MINIMUM_DAILY_HOURS <= hours <= MAXIMUM_DAILY_HOURS:
        errors["daily_hours_available"] = "Hours must be between 0 and 24."
        return None

    return hours


def _parse_yard_size(
    submission: ProfileSubmission, errors: dict[str, str]
) -> int | None:
    """Parse the optional yard size, which only applies when there is a yard."""
    if not submission.has_yard:
        # A size without a yard is contradictory input rather than a harmless
        # extra, so it is discarded rather than stored.
        return None

    raw_value = submission.yard_size_sqm
    if raw_value is None or not str(raw_value).strip():
        return None

    try:
        size = int(float(raw_value))
    except ValueError:
        errors["yard_size_sqm"] = "Enter the yard size as a number of square metres."
        return None

    if size < 0 or size > MAXIMUM_YARD_SIZE_SQM:
        errors["yard_size_sqm"] = "That yard size does not look right."
        return None

    return size


def _parse_child_age(
    submission: ProfileSubmission, errors: dict[str, str]
) -> int | None:
    """Parse the child age, which is required when children are present.

    The age materially changes matching: it is what decides whether an animal
    not certified with children is a hard disqualification. Leaving it blank
    would make the system fail safe and rule out animals unnecessarily, so it
    is required rather than optional.
    """
    if not submission.household_has_children:
        if submission.youngest_child_age and str(submission.youngest_child_age).strip():
            errors["youngest_child_age"] = (
                "You entered a child's age but did not say children live with you."
            )
        return None

    raw_value = submission.youngest_child_age
    if raw_value is None or not str(raw_value).strip():
        errors["youngest_child_age"] = (
            "Please give the age of your youngest child. It affects which animals suit you."
        )
        return None

    try:
        age = int(float(raw_value))
    except ValueError:
        errors["youngest_child_age"] = "Enter the age as a whole number."
        return None

    if not MINIMUM_CHILD_AGE <= age <= MAXIMUM_CHILD_AGE:
        errors["youngest_child_age"] = "Enter an age between 0 and 18."
        return None

    return age


def _parse_city(raw_value: str | None, errors: dict[str, str]) -> str | None:
    """Parse the required city field."""
    city = (raw_value or "").strip()
    if len(city) < MINIMUM_CITY_LENGTH:
        errors["city"] = "Please tell us which city you live in."
        return None
    if len(city) > MAXIMUM_CITY_LENGTH:
        errors["city"] = f"Please keep the city under {MAXIMUM_CITY_LENGTH} characters."
        return None
    return city


def _parse_species(
    raw_values: tuple[str, ...], errors: dict[str, str]
) -> tuple[Species, ...]:
    """Parse optional species preferences, rejecting unknown values.

    An unrecognised species is silently-wrong data rather than a harmless
    typo: it would never match anything, and the adopter would never know
    why their preferences had no effect.
    """
    parsed: list[Species] = []
    for raw_value in raw_values:
        if not raw_value.strip():
            continue
        try:
            parsed.append(Species(raw_value.strip()))
        except ValueError:
            errors["preferred_species"] = "One of the selected species is not recognised."
            return ()
    return tuple(parsed)


def _parse_other_animals_description(
    submission: ProfileSubmission, errors: dict[str, str]
) -> str | None:
    """Parse the optional description of existing pets."""
    if not submission.has_other_animals:
        return None

    description = (submission.other_animals_description or "").strip()
    if not description:
        return None
    if len(description) > MAXIMUM_DESCRIPTION_LENGTH:
        errors["other_animals_description"] = (
            f"Please keep this under {MAXIMUM_DESCRIPTION_LENGTH} characters."
        )
        return None
    return description
