"""Unit tests for animal validation (FR-4.1, FR-4.2, spec 5.2).

No request and no database: these rules decide what a valid animal is, and
they hold for any caller. The server-side check is the real one, because a
forged POST ignores the browser's `required` attributes entirely (NFR-5.2)
and the enum CHECK constraints on `animals` would answer a bad value with a
database error rather than a message a person can read.
"""

from __future__ import annotations

import pytest
from app.domain.animal_rules import (
    MAXIMUM_NAME_LENGTH,
    MAXIMUM_SPECIAL_NEEDS_LENGTH,
    AnimalSubmission,
    NoImageError,
    ensure_animal_has_an_image,
    validate_animal,
)
from app.domain.enums import ActivityLevel, AnimalSize, AnimalStatus, Species, Temperament

pytestmark = pytest.mark.unit


def submission(**overrides: object) -> AnimalSubmission:
    """Build a valid submission, overriding only the field under test."""
    defaults: dict[str, object] = {
        "name": "Milo",
        "species": "CAT",
        "breed": "Tabby",
        "age_years": "3",
        "size": "SMALL",
        "temperament": "CALM",
        "activity_level": "LOW",
        "required_space": "SMALL",
        "city": "Haifa",
        "status": "AVAILABLE",
        "description": "A quiet cat who likes windowsills.",
        "good_with_children": True,
        "good_with_other_animals": True,
        "has_special_needs": False,
        "special_needs_description": None,
        "image_urls": ("/static/uploads/milo.jpg",),
    }
    defaults.update(overrides)
    return AnimalSubmission(**defaults)  # type: ignore[arg-type]


class TestValidSubmissions:
    """A well-filled form becomes a typed animal."""

    def test_a_complete_submission_is_accepted(self) -> None:
        """Proves the happy path produces fully typed domain values."""
        result = validate_animal(submission())

        assert result.is_valid
        assert result.animal is not None
        assert result.animal.species is Species.CAT
        assert result.animal.size is AnimalSize.SMALL
        assert result.animal.temperament is Temperament.CALM
        assert result.animal.activity_level is ActivityLevel.LOW
        assert result.animal.status is AnimalStatus.AVAILABLE
        assert result.animal.age_years == 3.0

    def test_a_blank_status_defaults_to_available(self) -> None:
        """Proves listing an animal does not require choosing the obvious.

        A new animal is being listed, so AVAILABLE is the only sensible
        default and asking for it every time would be noise.
        """
        result = validate_animal(submission(status=None))

        assert result.is_valid
        assert result.animal is not None
        assert result.animal.status is AnimalStatus.AVAILABLE

    def test_a_breed_is_optional(self) -> None:
        """Proves a mixed-breed animal does not need an invented breed."""
        result = validate_animal(submission(breed=""))

        assert result.is_valid
        assert result.animal is not None
        assert result.animal.breed is None

    def test_a_fractional_age_is_kept(self) -> None:
        """Proves a six-month-old is representable."""
        result = validate_animal(submission(age_years="0.5"))

        assert result.is_valid
        assert result.animal is not None
        assert result.animal.age_years == 0.5

    def test_duplicate_photos_collapse(self) -> None:
        """Proves the same URL twice does not become two photos.

        The first photo is the primary one, and a duplicate would fight the
        filtered unique index that enforces exactly one primary per animal.
        """
        result = validate_animal(
            submission(image_urls=("/a.jpg", "/a.jpg", "/b.jpg"))
        )

        assert result.is_valid
        assert result.animal is not None
        assert result.animal.image_urls == ("/a.jpg", "/b.jpg")


class TestRequiredFields:
    """Missing required fields are reported, all at once."""

    @pytest.mark.parametrize(
        "field_name",
        ["name", "species", "size", "temperament", "activity_level",
         "required_space", "city", "age_years"],
    )
    def test_a_missing_required_field_is_rejected(self, field_name: str) -> None:
        """Proves each required field is genuinely required."""
        result = validate_animal(submission(**{field_name: None}))

        assert not result.is_valid
        assert field_name in result.errors

    def test_every_problem_is_reported_together(self) -> None:
        """Proves the form is fixable in one pass.

        Returning only the first error would make somebody with five
        mistakes reload five times to discover them.
        """
        result = validate_animal(
            submission(
                name="", species=None, size=None, city="   ", age_years="abc"
            )
        )

        assert not result.is_valid
        assert len(result.errors) == 5

    def test_a_blank_name_is_not_satisfied_by_whitespace(self) -> None:
        """Proves spaces do not count as a name."""
        result = validate_animal(submission(name="   "))

        assert not result.is_valid
        assert "name" in result.errors


class TestRejectedValues:
    """Values outside the vocabulary or the sensible range are refused."""

    def test_an_unknown_species_is_rejected(self) -> None:
        """Proves a forged post cannot store a species the enum lacks.

        The CHECK constraint on the column would refuse it anyway, but as a
        database error rather than a message anybody can act on.
        """
        result = validate_animal(submission(species="DRAGON"))

        assert not result.is_valid
        assert "species" in result.errors

    def test_an_unknown_status_is_rejected(self) -> None:
        """Proves the status vocabulary is closed too."""
        result = validate_animal(submission(status="PROBABLY_FINE"))

        assert not result.is_valid
        assert "status" in result.errors

    @pytest.mark.parametrize("age", ["-1", "41", "500"])
    def test_an_impossible_age_is_rejected(self, age: str) -> None:
        """Proves the age range is enforced rather than clamped."""
        result = validate_animal(submission(age_years=age))

        assert not result.is_valid
        assert "age_years" in result.errors

    def test_a_non_numeric_age_is_a_message_not_a_crash(self) -> None:
        """Proves bad input produces a message rather than an exception."""
        result = validate_animal(submission(age_years="about three"))

        assert not result.is_valid
        assert "age_years" in result.errors

    def test_a_name_longer_than_its_column_is_rejected(self) -> None:
        """Proves the length matches NVARCHAR(100).

        SQLite stores an oversized string happily and SQL Server 2014
        refuses it, so without this the form fails only against the real
        database, in front of a user.
        """
        result = validate_animal(submission(name="x" * (MAXIMUM_NAME_LENGTH + 1)))

        assert not result.is_valid
        assert "name" in result.errors


class TestSpecialNeeds:
    """The flag and its description are kept consistent."""

    def test_the_flag_requires_a_description(self) -> None:
        """Proves "has special needs" cannot be left unexplained.

        A bare flag tells an adopter nothing about what is involved.
        """
        result = validate_animal(
            submission(has_special_needs=True, special_needs_description="")
        )

        assert not result.is_valid
        assert "special_needs_description" in result.errors

    def test_a_description_without_the_flag_is_discarded(self) -> None:
        """Proves the database never holds a note about needs that do not exist."""
        result = validate_animal(
            submission(has_special_needs=False, special_needs_description="Blind")
        )

        assert result.is_valid
        assert result.animal is not None
        assert result.animal.special_needs_description is None

    def test_a_described_need_is_kept(self) -> None:
        """Proves a legitimate special need survives validation."""
        result = validate_animal(
            submission(
                has_special_needs=True,
                special_needs_description="Needs daily medication for arthritis.",
            )
        )

        assert result.is_valid
        assert result.animal is not None
        assert result.animal.has_special_needs
        assert result.animal.special_needs_description is not None

    def test_an_overlong_description_is_rejected(self) -> None:
        """Proves the note cannot exceed its column width."""
        result = validate_animal(
            submission(
                has_special_needs=True,
                special_needs_description="x" * (MAXIMUM_SPECIAL_NEEDS_LENGTH + 1),
            )
        )

        assert not result.is_valid
        assert "special_needs_description" in result.errors


class TestTheImageRule:
    """FR-4.2: every animal must have at least one photograph."""

    def test_a_submission_with_no_photo_is_rejected(self) -> None:
        """Proves the rule is enforced at validation, with a readable message."""
        result = validate_animal(submission(image_urls=()))

        assert not result.is_valid
        assert "image_urls" in result.errors

    def test_blank_photo_entries_do_not_count(self) -> None:
        """Proves whitespace does not satisfy the requirement."""
        result = validate_animal(submission(image_urls=("", "   ")))

        assert not result.is_valid
        assert "image_urls" in result.errors

    def test_the_guard_raises_for_an_empty_set(self) -> None:
        """Proves the write-side guard refuses independently of the form.

        This rule cannot be a table constraint - it is a cross-table
        cardinality rule - so the only thing enforcing it is this function,
        and it is called on every write rather than only on creation.
        """
        with pytest.raises(NoImageError):
            ensure_animal_has_an_image(())

    def test_the_guard_accepts_one_photo(self) -> None:
        """Proves a single photograph is enough."""
        ensure_animal_has_an_image(("/static/uploads/milo.jpg",))
