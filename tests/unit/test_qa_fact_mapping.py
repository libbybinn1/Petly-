"""Adversarial tests for the places stored values become domain facts.

`app/cqrs/queries/match_queries._adopter_facts` and
`agent_service/loop.build_adopter_facts` both convert untrusted stored or
transported values into `AdopterFacts`. Both swallow unrecognised values and
fall back to a default. That is the right instinct for most fields and the
wrong one for at least one, which is what these tests pin down.

Both are now thin wrappers over `app.domain.facts`, and the last class here
is what holds them to that: they were once separate mappings, they drifted,
and the agent recomputed a score the web tier disagreed with.

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
from app.domain.facts import split_stored_values
from app.domain.matching import (
    NEUTRAL_SCORE,
    PERFECT_SCORE,
    AgePreference,
    Criterion,
    CriterionScore,
    MatchScore,
    calculate_match_score,
)
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


def mcp_adopter_payload(profile: AdopterProfile) -> dict[str, object]:
    """Shape one profile row the way `mcp_server.server.get_adopter_profile` does.

    Written out rather than imported: importing the tool server opens a
    database engine at module scope, and this is a unit test. Spelling the
    payload out also pins the contract - if the tool ever stops sending a
    field the scorer needs, this list is where it shows up.
    """
    return {
        "found": True,
        "adopter_profile_id": profile.adopter_profile_id,
        "home_type": profile.home_type,
        "has_yard": bool(profile.has_yard),
        "yard_size_sqm": profile.yard_size_sqm,
        "household_has_children": bool(profile.household_has_children),
        "youngest_child_age": profile.youngest_child_age,
        "has_other_animals": bool(profile.has_other_animals),
        "other_animals_description": profile.other_animals_description,
        "experience_level": profile.experience_level,
        "activity_level": profile.activity_level,
        "daily_hours_available": float(profile.daily_hours_available),
        "city": profile.city,
        "preferred_species": split_stored_values(profile.preferred_species),
        "preferred_size": profile.preferred_size,
        "preferred_age_range": profile.preferred_age_range,
        "open_to_proactive_suggestions": bool(profile.open_to_proactive_suggestions),
        "is_complete": bool(profile.is_complete),
    }


def mcp_animal_payload(animal: Animal) -> dict[str, object]:
    """Shape one animal row the way `mcp_server.server.get_animal_profile` does."""
    return {
        "found": True,
        "animal_id": animal.animal_id,
        "name": animal.name,
        "species": animal.species,
        "breed": animal.breed,
        "age_years": float(animal.age_years),
        "size": animal.size,
        "temperament": animal.temperament,
        "activity_level": animal.activity_level,
        "good_with_children": bool(animal.good_with_children),
        "good_with_other_animals": bool(animal.good_with_other_animals),
        "has_special_needs": bool(animal.has_special_needs),
        "special_needs_description": animal.special_needs_description,
        "required_space": animal.required_space,
        "city": animal.city,
        "status": animal.status,
        "description": animal.description,
    }


# The profile that reproduced the divergence: it states both preferences, and
# the agent's own mapping read neither of them.
PARTICULAR_PROFILE: dict[str, object] = {
    "home_type": HomeType.APARTMENT.value,
    "has_yard": False,
    "has_other_animals": True,
    "experience_level": ExperienceLevel.NONE.value,
    "activity_level": ActivityLevel.MODERATE.value,
    "daily_hours_available": 2.0,
    "preferred_species": "DOG",
    "preferred_age_range": AgePreference.ADULT.value,
    "preferred_size": AnimalSize.MEDIUM.value,
}
PARTICULAR_ANIMAL: dict[str, object] = {
    "species": Species.DOG.value,
    "age_years": 3.0,
    "size": AnimalSize.SMALL.value,
    "required_space": AnimalSize.SMALL.value,
}

# What that pairing scores, and what it scored while the age preference was
# being dropped. Both pinned: the point of the fix is that one number is now
# unreachable, and a test that only compared the two tiers would still pass if
# both of them silently started ignoring the preference again.
SCORE_WITH_THE_STATED_PREFERENCES = 95
SCORE_WHEN_THE_AGE_PREFERENCE_WAS_DROPPED = 91


class TestTheTwoTiersAgree:
    """The web tier reads a row; the agent reads an MCP payload of that row.

    Two routes to the same facts. They were two separate mappings, and two of
    the three copies never set `preferred_age_range` or `preferred_size`.
    """

    def test_both_tiers_build_identical_adopter_facts(self) -> None:
        """Proves one record cannot produce two different AdopterFacts.

        This is the regression: the agent's own mapping omitted two fields,
        so the same adopter was a different person depending on which tier
        asked.
        """
        profile = make_profile_row(**PARTICULAR_PROFILE)

        assert _adopter_facts(profile) == build_adopter_facts(mcp_adopter_payload(profile))

    def test_both_tiers_build_identical_animal_facts(self) -> None:
        """Proves the animal half of the conversion agrees across the tiers too."""
        animal = make_animal_row(**PARTICULAR_ANIMAL)

        assert _animal_facts(animal) == build_animal_facts(mcp_animal_payload(animal))

    def test_both_tiers_read_the_stated_preferences(self) -> None:
        """Proves the two fields that were being dropped now arrive on both routes."""
        profile = make_profile_row(**PARTICULAR_PROFILE)
        both = (_adopter_facts(profile), build_adopter_facts(mcp_adopter_payload(profile)))

        for facts in both:
            assert facts.preferred_age_range is AgePreference.ADULT
            assert facts.preferred_size is AnimalSize.MEDIUM

    def test_the_agent_recomputes_the_score_the_web_tier_showed(self) -> None:
        """Proves the reported 95-against-91 divergence is gone.

        The adopter asked for an animal aged 2-8 and the animal is three, so
        the age criterion is worth its full 100 rather than the neutral 60 an
        unstated preference earns. Nine percent of the weight moved by 40
        points, which is the four the two tiers differed by.
        """
        profile = make_profile_row(**PARTICULAR_PROFILE)
        animal = make_animal_row(**PARTICULAR_ANIMAL)

        from_the_web = calculate_match_score(
            _adopter_facts(profile), _animal_facts(animal), MatchDirection.ADOPTER_TO_ANIMAL
        )
        from_the_agent = calculate_match_score(
            build_adopter_facts(mcp_adopter_payload(profile)),
            build_animal_facts(mcp_animal_payload(animal)),
            MatchDirection.ADOPTER_TO_ANIMAL,
        )

        assert from_the_agent.score == from_the_web.score
        assert from_the_web.score == SCORE_WITH_THE_STATED_PREFERENCES
        assert from_the_web.score != SCORE_WHEN_THE_AGE_PREFERENCE_WAS_DROPPED

    def test_dropping_the_age_preference_is_what_cost_the_four_points(self) -> None:
        """Proves the pinned numbers describe this defect and not some other one.

        Negative half of the pair: the same pairing with the preference
        removed still scores what the agent used to report, so the two
        constants above are anchored to the actual cause.
        """
        without_preference = dict(PARTICULAR_PROFILE)
        without_preference["preferred_age_range"] = None

        score = calculate_match_score(
            _adopter_facts(make_profile_row(**without_preference)),
            _animal_facts(make_animal_row(**PARTICULAR_ANIMAL)),
            MatchDirection.ADOPTER_TO_ANIMAL,
        )

        assert score.score == SCORE_WHEN_THE_AGE_PREFERENCE_WAS_DROPPED


class TestAnUnreadableAnimalSpecies:
    """An animal must carry a species; an adopter's preference may be absent."""

    def test_a_corrupt_animal_species_is_recorded_as_unknown(self) -> None:
        """Proves an unparseable species is flagged rather than filed under OTHER.

        OTHER is a real catalogue category, so classifying a corrupt value as
        one would be an assertion about the animal that nothing supports.
        """
        facts = _animal_facts(make_animal_row(species="DRAGON"))

        assert facts.species_is_known is False

    def test_an_unknown_species_scores_neutral_against_a_preference(self) -> None:
        """Proves a corrupt species cannot perfect-score an adopter who wants OTHER.

        The heaviest ADOPTER_TO_ANIMAL criterion would otherwise reward a
        pairing nobody can vouch for.
        """
        adopter = _adopter_facts(make_profile_row(preferred_species="OTHER"))
        unreadable = _animal_facts(make_animal_row(species="DRAGON"))

        score = calculate_match_score(adopter, unreadable, MatchDirection.ADOPTER_TO_ANIMAL)

        assert _species_criterion(score).score == NEUTRAL_SCORE

    def test_a_readable_other_still_matches_an_other_preference(self) -> None:
        """Proves the flag did not break the legitimate OTHER-to-OTHER match."""
        adopter = _adopter_facts(make_profile_row(preferred_species="OTHER"))
        exotic = _animal_facts(make_animal_row(species=Species.OTHER.value))

        score = calculate_match_score(adopter, exotic, MatchDirection.ADOPTER_TO_ANIMAL)

        assert _species_criterion(score).score == PERFECT_SCORE


def _species_criterion(score: MatchScore) -> CriterionScore:
    """Pull the species criterion out of a breakdown."""
    return next(
        item for item in score.criterion_scores
        if item.criterion is Criterion.SPECIES_PREFERENCE
    )
