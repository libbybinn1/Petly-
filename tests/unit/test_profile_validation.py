"""Unit tests for adopter profile validation (spec section 5.1).

The server-side check is the real one: a browser's `required` attribute is a
convenience that a forged POST ignores entirely (NFR-5.2). These tests
exercise the rules directly, with no request and no database, so they hold
for any caller.
"""

from __future__ import annotations

import pytest
from app.domain.enums import ActivityLevel, ExperienceLevel, HomeType, Species
from app.domain.profile_rules import (
    MAXIMUM_DESCRIPTION_LENGTH,
    ProfileSubmission,
    validate_profile,
)

pytestmark = pytest.mark.unit


def submission(**overrides: object) -> ProfileSubmission:
    """Build a valid submission, overriding only the field under test."""
    defaults: dict[str, object] = {
        "home_type": "HOUSE",
        "has_yard": True,
        "yard_size_sqm": "80",
        "household_has_children": False,
        "youngest_child_age": None,
        "has_other_animals": False,
        "other_animals_description": None,
        "experience_level": "SOME",
        "activity_level": "MODERATE",
        "daily_hours_available": "3.5",
        "city": "Haifa",
        "preferred_species": ("DOG", "CAT"),
        "open_to_proactive_suggestions": True,
    }
    defaults.update(overrides)
    return ProfileSubmission(**defaults)  # type: ignore[arg-type]


class TestValidSubmissions:
    """A well-filled form parses into typed domain values."""

    def test_complete_submission_is_accepted(self) -> None:
        """Proves the happy path produces a fully typed profile."""
        result = validate_profile(submission())

        assert result.is_valid
        assert result.profile is not None
        assert result.profile.home_type is HomeType.HOUSE
        assert result.profile.experience_level is ExperienceLevel.SOME
        assert result.profile.activity_level is ActivityLevel.MODERATE
        assert result.profile.daily_hours_available == 3.5
        assert result.profile.preferred_species == (Species.DOG, Species.CAT)

    def test_species_preferences_are_optional(self) -> None:
        """Proves an adopter open to anything can say so by selecting nothing."""
        result = validate_profile(submission(preferred_species=()))

        assert result.is_valid
        assert result.profile is not None
        assert result.profile.preferred_species == ()

    def test_zero_hours_is_accepted(self) -> None:
        """Proves an honest zero is valid input, not a missing value.

        Rejecting it would push somebody with no time to overstate it, which
        is worse for the animal than recording the truth and scoring low.
        """
        result = validate_profile(submission(daily_hours_available="0"))
        assert result.is_valid


class TestRequiredFields:
    """Missing required fields are reported, all at once."""

    @pytest.mark.parametrize(
        "field_name",
        ["home_type", "experience_level", "activity_level", "daily_hours_available"],
    )
    def test_missing_required_field_is_rejected(self, field_name: str) -> None:
        """Proves each required field is genuinely required."""
        result = validate_profile(submission(**{field_name: None}))

        assert not result.is_valid
        assert field_name in result.errors

    def test_blank_city_is_rejected(self) -> None:
        """Proves whitespace does not satisfy a required text field."""
        result = validate_profile(submission(city="   "))

        assert not result.is_valid
        assert "city" in result.errors

    def test_every_problem_is_reported_together(self) -> None:
        """Proves the form is fixable in one pass.

        Returning only the first error would make a user with four mistakes
        reload four times to discover them.
        """
        result = validate_profile(
            submission(
                home_type=None,
                experience_level=None,
                activity_level=None,
                city="",
                daily_hours_available=None,
            )
        )

        assert not result.is_valid
        assert len(result.errors) == 5


class TestNumericRanges:
    """Numbers outside their range are rejected rather than clamped."""

    @pytest.mark.parametrize("value", ["-1", "25", "100"])
    def test_hours_outside_a_day_are_rejected(self, value: str) -> None:
        """Proves the daily-hours field cannot hold an impossible figure."""
        result = validate_profile(submission(daily_hours_available=value))

        assert not result.is_valid
        assert "daily_hours_available" in result.errors

    def test_non_numeric_hours_is_a_validation_error_not_a_crash(self) -> None:
        """Proves bad input produces a message rather than an exception.

        A forged POST can send anything; the parse must fail politely.
        """
        result = validate_profile(submission(daily_hours_available="lots"))

        assert not result.is_valid
        assert "daily_hours_available" in result.errors

    def test_unknown_enum_value_is_rejected(self) -> None:
        """Proves a hand-crafted POST cannot store an invalid home type."""
        result = validate_profile(submission(home_type="CASTLE"))

        assert not result.is_valid
        assert "home_type" in result.errors

    def test_unknown_species_is_rejected(self) -> None:
        """Proves an unrecognised species is refused rather than silently dropped.

        Accepting it would leave the adopter with a preference that never
        matches anything and no indication why.
        """
        result = validate_profile(submission(preferred_species=("DOG", "DRAGON")))

        assert not result.is_valid
        assert "preferred_species" in result.errors


class TestHouseholdConsistency:
    """Fields that depend on each other are checked together."""

    def test_children_require_an_age(self) -> None:
        """Proves the age is required when children are present.

        The age decides whether an animal not certified with children is a
        hard disqualification, so a blank would make matching fail safe and
        rule out animals unnecessarily.
        """
        result = validate_profile(
            submission(household_has_children=True, youngest_child_age=None)
        )

        assert not result.is_valid
        assert "youngest_child_age" in result.errors

    def test_age_without_children_is_rejected_as_contradictory(self) -> None:
        """Proves inconsistent input is caught rather than half-stored."""
        result = validate_profile(
            submission(household_has_children=False, youngest_child_age="7")
        )

        assert not result.is_valid
        assert "youngest_child_age" in result.errors

    @pytest.mark.parametrize("age", ["-1", "19", "45"])
    def test_implausible_child_age_is_rejected(self, age: str) -> None:
        """Proves the age range is enforced."""
        result = validate_profile(
            submission(household_has_children=True, youngest_child_age=age)
        )

        assert not result.is_valid
        assert "youngest_child_age" in result.errors

    def test_valid_child_age_is_accepted(self) -> None:
        """Proves a legitimate household with children passes."""
        result = validate_profile(
            submission(household_has_children=True, youngest_child_age="6")
        )

        assert result.is_valid
        assert result.profile is not None
        assert result.profile.youngest_child_age == 6

    def test_yard_size_is_discarded_without_a_yard(self) -> None:
        """Proves contradictory input is dropped rather than stored.

        Storing a yard size for a home with no yard would leave the database
        holding a fact that is not true.
        """
        result = validate_profile(submission(has_yard=False, yard_size_sqm="200"))

        assert result.is_valid
        assert result.profile is not None
        assert result.profile.yard_size_sqm is None

    def test_pet_description_is_discarded_without_pets(self) -> None:
        """Proves the same rule applies to the other dependent field."""
        result = validate_profile(
            submission(has_other_animals=False, other_animals_description="A cat")
        )

        assert result.is_valid
        assert result.profile is not None
        assert result.profile.other_animals_description is None

    def test_overlong_pet_description_is_rejected(self) -> None:
        """Proves a long free-text field cannot exceed its column width."""
        result = validate_profile(
            submission(
                has_other_animals=True,
                other_animals_description="x" * (MAXIMUM_DESCRIPTION_LENGTH + 1),
            )
        )

        assert not result.is_valid
        assert "other_animals_description" in result.errors


class TestProactiveOptIn:
    """The opt-in of spec 5.1 defaults to off and is never inferred."""

    def test_opt_in_defaults_to_false(self) -> None:
        """Proves an unticked box means no, not unspecified.

        This is the field that decides whether staff may contact somebody who
        never applied, so defaulting it on would be a privacy failure.
        """
        assert ProfileSubmission().open_to_proactive_suggestions is False

    def test_opt_in_is_recorded_when_chosen(self) -> None:
        """Proves a deliberate opt-in is stored."""
        result = validate_profile(submission(open_to_proactive_suggestions=True))

        assert result.is_valid
        assert result.profile is not None
        assert result.profile.open_to_proactive_suggestions is True

    def test_opt_out_is_recorded_when_not_chosen(self) -> None:
        """Proves declining is stored as declining."""
        result = validate_profile(submission(open_to_proactive_suggestions=False))

        assert result.is_valid
        assert result.profile is not None
        assert result.profile.open_to_proactive_suggestions is False

    def test_opting_out_does_not_block_saving(self) -> None:
        """Proves an adopter can have a complete profile without opting in.

        Completeness and consent are separate: somebody may want personal
        matches without wanting to be contacted.
        """
        result = validate_profile(submission(open_to_proactive_suggestions=False))
        assert result.is_valid
