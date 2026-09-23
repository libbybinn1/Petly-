"""Adversarial tests for the two places stored values become domain facts.

`app/cqrs/queries/match_queries._adopter_facts` and
`agent_service/loop.build_adopter_facts` both convert untrusted stored or
transported values into `AdopterFacts`. Both swallow unrecognised values and
fall back to a default. That is the right instinct for most fields and the
wrong one for at least one, which is what these tests pin down.

No database is opened: the ORM objects are constructed in memory and never
attached to a session.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from agent_service.loop import build_adopter_facts, build_animal_facts
from app.cqrs.queries.match_queries import _adopter_facts, _animal_facts
from app.domain.enums import (
    ActivityLevel,
    AnimalSize,
    ExperienceLevel,
    HomeType,
    MatchDirection,
    Species,
    Temperament,
)
from app.domain.matching import calculate_match_score
from app.infrastructure.models import AdopterProfile, Animal

pytestmark = pytest.mark.unit

NOW = datetime.now(UTC).replace(tzinfo=None)


def make_profile_row(**overrides: object) -> AdopterProfile:
    """Build a detached AdopterProfile row. No session, no database."""
    defaults: dict[str, object] = {
        "adopter_profile_id": "profile-1",
        "user_id": "user-1",
        "home_type": HomeType.HOUSE.value,
        "has_yard": True,
        "household_has_children": False,
        "youngest_child_age": None,
        "has_other_animals": False,
        "experience_level": ExperienceLevel.SOME.value,
        "activity_level": ActivityLevel.MODERATE.value,
        "daily_hours_available": 4.0,
        "city": "Haifa",
        "preferred_species": None,
        "open_to_proactive_suggestions": True,
        "is_complete": True,
        "created_at": NOW,
        "updated_at": NOW,
    }
    defaults.update(overrides)
    return AdopterProfile(**defaults)


def make_animal_row(**overrides: object) -> Animal:
    """Build a detached Animal row. No session, no database."""
    defaults: dict[str, object] = {
        "animal_id": "animal-1",
        "name": "Rex",
        "species": Species.DOG.value,
        "age_years": 3.0,
        "size": AnimalSize.MEDIUM.value,
        "temperament": Temperament.BALANCED.value,
        "activity_level": ActivityLevel.MODERATE.value,
        "good_with_children": True,
        "good_with_other_animals": True,
        "has_special_needs": False,
        "required_space": AnimalSize.MEDIUM.value,
        "city": "Haifa",
        "status": "AVAILABLE",
        "created_at": NOW,
        "updated_at": NOW,
    }
    defaults.update(overrides)
    return Animal(**defaults)


class TestStoredSpeciesPreferences:
    """`preferred_species` is a comma-separated string in one NVARCHAR column."""

    def test_an_empty_column_means_no_preference(self) -> None:
        """Proves NULL and '' both read back as 'no constraint', not as a bad enum."""
        assert _adopter_facts(make_profile_row(preferred_species=None)).preferred_species == (
            frozenset()
        )
        assert _adopter_facts(make_profile_row(preferred_species="")).preferred_species == (
            frozenset()
        )

    def test_stray_commas_and_spaces_are_tolerated(self) -> None:
        """Proves 'CAT, DOG,,' parses to exactly the two species it names."""
        facts = _adopter_facts(make_profile_row(preferred_species="CAT, DOG,,"))

        assert facts.preferred_species == frozenset({Species.CAT, Species.DOG})

    def test_duplicates_collapse_because_the_field_is_a_set(self) -> None:
        """Proves 'DOG,DOG,DOG' cannot weight the species criterion three times."""
        facts = _adopter_facts(make_profile_row(preferred_species="DOG,DOG,DOG"))

        assert facts.preferred_species == frozenset({Species.DOG})

    def test_lowercase_is_not_recognised_as_a_species(self) -> None:
        """Documents that stored values are case-sensitive, matching the CHECK constraint."""
        facts = _adopter_facts(make_profile_row(preferred_species="dog"))

        assert Species.DOG not in facts.preferred_species

    def test_an_unrecognised_species_is_dropped_not_turned_into_other(self) -> None:
        """Proves a corrupt preference cannot invent a preference for OTHER animals.

        It used to: an unparseable value fell back to `Species.OTHER`, which
        is a preference an adopter can genuinely hold rather than a marker
        for "unknown".
        """
        facts = _adopter_facts(make_profile_row(preferred_species="PONY"))

        assert Species.OTHER not in facts.preferred_species
        assert facts.preferred_species == frozenset()

    def test_a_dropped_preference_no_longer_perfect_scores_an_other_animal(self) -> None:
        """Proves the harm the fallback caused is gone.

        Species preference is the heaviest ADOPTER_TO_ANIMAL criterion, so
        the invented preference scored every OTHER-species animal 100 on it.
        With nothing recorded, the adopter is treated as unconstrained
        instead of as actively wanting such an animal.
        """
        facts = _adopter_facts(make_profile_row(preferred_species="PONY"))
        exotic = _animal_facts(make_animal_row(species=Species.OTHER.value))

        score = calculate_match_score(facts, exotic, MatchDirection.ADOPTER_TO_ANIMAL)
        species_criterion = next(
            item for item in score.criterion_scores
            if item.criterion.value == "species_preference"
        )

        assert species_criterion.score < 100

    def test_a_real_other_preference_is_still_kept(self) -> None:
        """Proves dropping bad values did not also drop a legitimate OTHER.

        An adopter open to an unusual animal chose that deliberately, and
        the fix must not confuse their choice with a parse failure.
        """
        facts = _adopter_facts(make_profile_row(preferred_species="OTHER"))

        assert facts.preferred_species == frozenset({Species.OTHER})

    def test_the_agent_payload_path_drops_it_too(self) -> None:
        """Proves both conversion paths agree; fixing one only would diverge.

        The web tier reads a comma-separated column and the agent reads an
        MCP payload. Two routes to the same facts must not score the same
        adopter differently.
        """
        facts = build_adopter_facts({"preferred_species": ["PONY"]})

        assert facts.preferred_species == frozenset()

    def test_the_agent_payload_path_keeps_recognised_species(self) -> None:
        """Proves the agent path still reads the valid entries in a mixed list."""
        facts = build_adopter_facts({"preferred_species": ["DOG", "PONY", "CAT"]})

        assert facts.preferred_species == frozenset({Species.DOG, Species.CAT})


class TestOtherStoredEnumFallbacks:
    """Unknown values elsewhere fall back silently, which is a scoring risk."""

    def test_an_unknown_home_type_is_scored_as_an_apartment(self) -> None:
        """Documents that corrupt home_type data scores as the most restrictive home.

        Failing towards the smaller home is the safe direction, so this is
        recorded as accepted behaviour rather than reported as a defect.
        """
        facts = _adopter_facts(make_profile_row(home_type="MANSION"))

        assert facts.home_type is HomeType.APARTMENT

    def test_an_unknown_required_space_is_scored_as_medium(self) -> None:
        """Documents that corrupt required_space defaults to MEDIUM, not LARGE.

        This one fails towards the *permissive* side: a LARGE animal whose
        required_space is corrupt stops being disqualified from a yardless
        apartment. Worth knowing, but the CHECK constraint makes it
        unreachable through the application.
        """
        facts = _animal_facts(make_animal_row(required_space="ENORMOUS"))

        assert facts.required_space is AnimalSize.MEDIUM

    def test_a_null_city_becomes_an_empty_string_rather_than_none(self) -> None:
        """Proves the NOT NULL city column's absence cannot crash location scoring."""
        assert _adopter_facts(make_profile_row(city=None)).city == ""
        assert _animal_facts(make_animal_row(city=None)).city == ""

    def test_a_null_daily_hours_becomes_zero(self) -> None:
        """Proves a missing availability value scores as no time, not as a TypeError."""
        facts = _adopter_facts(make_profile_row(daily_hours_available=None))

        assert facts.daily_hours_available == 0.0


class TestAgentPayloadTypeCoercion:
    """The MCP payload arrives as JSON, so a field's type is not guaranteed."""

    def test_absent_fields_use_safe_defaults(self) -> None:
        """Proves an empty payload still produces a scorable fact object."""
        facts = build_adopter_facts({})
        animal = build_animal_facts({})

        assert calculate_match_score(
            facts, animal, MatchDirection.ANIMAL_TO_ADOPTER
        ).score >= 0

    def test_a_string_child_age_does_not_crash_scoring(self) -> None:
        """Proves a mistyped payload field degrades gracefully rather than raising.

        It used to raise TypeError from inside the child-safety rule,
        failing the whole analysis job. Every other numeric field in the
        payload was coerced; this one was passed through untouched.
        """
        adopter = build_adopter_facts(
            {"household_has_children": True, "youngest_child_age": "5"}
        )
        animal = build_animal_facts({"good_with_children": False})

        score = calculate_match_score(adopter, animal, MatchDirection.ANIMAL_TO_ADOPTER)

        assert score.is_disqualified is True

    def test_a_string_child_age_is_read_as_the_number_it_spells(self) -> None:
        """Proves coercion preserves the value rather than discarding it.

        Degrading to None would be safe but wrong: a five-year-old in the
        household is exactly the fact the safety rule needs.
        """
        adopter = build_adopter_facts(
            {"household_has_children": True, "youngest_child_age": "5"}
        )

        assert adopter.youngest_child_age == 5

    def test_an_unreadable_child_age_becomes_not_stated(self) -> None:
        """Proves a value that is not a number at all degrades instead of crashing."""
        adopter = build_adopter_facts(
            {"household_has_children": True, "youngest_child_age": "unknown"}
        )

        assert adopter.youngest_child_age is None

    def test_a_string_numeric_for_hours_is_coerced(self) -> None:
        """Proves daily_hours_available is coerced, unlike the child age."""
        facts = build_adopter_facts({"daily_hours_available": "2.5"})

        assert facts.daily_hours_available == 2.5
