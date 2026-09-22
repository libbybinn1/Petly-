"""Unit tests for the deterministic matching engine.

These tests run with no database, no network and no language model. That is
the point: spec section 8 requires scores that can be reproduced and
explained, so the arithmetic must be verifiable in isolation.
"""

from __future__ import annotations

import pytest
from app.domain.enums import (
    ActivityLevel,
    AnimalSize,
    ExperienceLevel,
    HomeType,
    MatchDirection,
    Species,
    Temperament,
)
from app.domain.matching import (
    ADOPTER_TO_ANIMAL_WEIGHTS,
    ANIMAL_TO_ADOPTER_WEIGHTS,
    AdopterFacts,
    AnimalFacts,
    Criterion,
    calculate_match_score,
    find_hard_constraint_violation,
    rank_adopters_for_animal,
    rank_animals_for_adopter,
    score_daily_availability,
    score_experience_level,
    score_species_preference,
)

pytestmark = pytest.mark.unit


def make_adopter(**overrides: object) -> AdopterFacts:
    """Build an adopter with sensible defaults, overriding only what matters.

    Keeping defaults in one place means each test shows only the attribute it
    is actually about.
    """
    defaults: dict[str, object] = {
        "home_type": HomeType.HOUSE,
        "has_yard": True,
        "household_has_children": False,
        "youngest_child_age": None,
        "has_other_animals": False,
        "experience_level": ExperienceLevel.SOME,
        "activity_level": ActivityLevel.MODERATE,
        "daily_hours_available": 4.0,
        "city": "Haifa",
        "preferred_species": frozenset({Species.DOG}),
        "open_to_proactive_suggestions": True,
        "is_complete": True,
    }
    defaults.update(overrides)
    return AdopterFacts(**defaults)  # type: ignore[arg-type]


def make_animal(**overrides: object) -> AnimalFacts:
    """Build an animal with sensible defaults."""
    defaults: dict[str, object] = {
        "species": Species.DOG,
        "age_years": 3.0,
        "size": AnimalSize.MEDIUM,
        "temperament": Temperament.BALANCED,
        "activity_level": ActivityLevel.MODERATE,
        "good_with_children": True,
        "good_with_other_animals": True,
        "has_special_needs": False,
        "required_space": AnimalSize.MEDIUM,
        "city": "Haifa",
    }
    defaults.update(overrides)
    return AnimalFacts(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------- determinism


class TestDeterminism:
    """Scoring must be reproducible - the core promise of spec section 8."""

    def test_identical_input_gives_identical_score(self) -> None:
        """Proves the same pair scores the same every time, with no LLM involved."""
        adopter, animal = make_adopter(), make_animal()

        scores = {
            calculate_match_score(adopter, animal, MatchDirection.ADOPTER_TO_ANIMAL).score
            for _ in range(20)
        }

        assert len(scores) == 1, "scoring is not deterministic"

    def test_score_is_within_valid_range(self) -> None:
        """Proves the score always falls in 0-100, as the database CHECK requires."""
        combinations = [
            (make_adopter(), make_animal()),
            (make_adopter(home_type=HomeType.APARTMENT, has_yard=False,
                          daily_hours_available=0.5, experience_level=ExperienceLevel.NONE),
             make_animal(activity_level=ActivityLevel.HIGH, has_special_needs=True)),
            (make_adopter(home_type=HomeType.FARM, daily_hours_available=12.0,
                          experience_level=ExperienceLevel.EXPERIENCED),
             make_animal(size=AnimalSize.SMALL, activity_level=ActivityLevel.LOW)),
        ]

        for adopter, animal in combinations:
            for direction in MatchDirection:
                result = calculate_match_score(adopter, animal, direction)
                assert 0 <= result.score <= 100


class TestWeights:
    """The two directions must genuinely differ (spec section 9)."""

    def test_weight_sets_each_sum_to_one(self) -> None:
        """Proves the weighted average cannot exceed 100 or fall short of 0."""
        assert sum(ADOPTER_TO_ANIMAL_WEIGHTS.values()) == pytest.approx(1.0)
        assert sum(ANIMAL_TO_ADOPTER_WEIGHTS.values()) == pytest.approx(1.0)

    def test_both_weight_sets_cover_every_criterion(self) -> None:
        """Proves no criterion is silently dropped from one direction."""
        assert set(ADOPTER_TO_ANIMAL_WEIGHTS) == set(Criterion)
        assert set(ANIMAL_TO_ADOPTER_WEIGHTS) == set(Criterion)

    def test_directions_weight_species_preference_differently(self) -> None:
        """Proves the asymmetry spec section 9.2 calls for.

        From the animal's point of view, whether someone *wanted* this species
        matters far less than whether they can meet its needs.
        """
        adopter_side = ADOPTER_TO_ANIMAL_WEIGHTS[Criterion.SPECIES_PREFERENCE]
        animal_side = ANIMAL_TO_ADOPTER_WEIGHTS[Criterion.SPECIES_PREFERENCE]

        assert adopter_side > animal_side * 5

    def test_same_pair_can_score_differently_by_direction(self) -> None:
        """Proves direction changes the outcome, not just the label."""
        # Wanted the species, but has little time and no experience: good from
        # the adopter's point of view, worrying from the animal's.
        adopter = make_adopter(
            preferred_species=frozenset({Species.DOG}),
            daily_hours_available=1.0,
            experience_level=ExperienceLevel.NONE,
        )
        animal = make_animal(activity_level=ActivityLevel.HIGH, has_special_needs=True)

        adopter_view = calculate_match_score(adopter, animal, MatchDirection.ADOPTER_TO_ANIMAL)
        animal_view = calculate_match_score(adopter, animal, MatchDirection.ANIMAL_TO_ADOPTER)

        assert adopter_view.score != animal_view.score
        assert animal_view.score < adopter_view.score


# ------------------------------------------------------------ hard constraints


class TestHardConstraints:
    """Hard constraints disqualify outright; they are not tradeable."""

    def test_unsafe_child_situation_disqualifies(self) -> None:
        """Proves a young child plus a not-child-safe animal is refused."""
        adopter = make_adopter(household_has_children=True, youngest_child_age=4)
        animal = make_animal(good_with_children=False)

        assert find_hard_constraint_violation(adopter, animal) is not None

    def test_unknown_child_age_is_treated_as_risky(self) -> None:
        """Proves an unknown child age fails safe rather than being assumed fine."""
        adopter = make_adopter(household_has_children=True, youngest_child_age=None)
        animal = make_animal(good_with_children=False)

        assert find_hard_constraint_violation(adopter, animal) is not None

    def test_older_child_does_not_disqualify(self) -> None:
        """Proves the age limit is applied, not merely the presence of children."""
        adopter = make_adopter(household_has_children=True, youngest_child_age=15)
        animal = make_animal(good_with_children=False)

        assert find_hard_constraint_violation(adopter, animal) is None

    def test_resident_animals_plus_solo_animal_disqualifies(self) -> None:
        """Proves an only-pet animal is not placed into a multi-pet household."""
        adopter = make_adopter(has_other_animals=True)
        animal = make_animal(good_with_other_animals=False)

        assert find_hard_constraint_violation(adopter, animal) is not None

    def test_large_animal_in_yardless_apartment_disqualifies(self) -> None:
        """Proves the welfare space limit is enforced."""
        adopter = make_adopter(home_type=HomeType.APARTMENT, has_yard=False)
        animal = make_animal(required_space=AnimalSize.LARGE)

        assert find_hard_constraint_violation(adopter, animal) is not None

    def test_a_yard_lifts_the_apartment_space_limit(self) -> None:
        """Proves outdoor space is counted, not just the dwelling type."""
        adopter = make_adopter(home_type=HomeType.APARTMENT, has_yard=True)
        animal = make_animal(required_space=AnimalSize.LARGE)

        assert find_hard_constraint_violation(adopter, animal) is None

    def test_disqualification_scores_zero_and_carries_a_reason(self) -> None:
        """Proves a violation cannot be masked by strong soft scores."""
        adopter = make_adopter(
            has_other_animals=True,
            home_type=HomeType.FARM,
            daily_hours_available=12.0,
            experience_level=ExperienceLevel.EXPERIENCED,
        )
        animal = make_animal(good_with_other_animals=False)

        result = calculate_match_score(adopter, animal, MatchDirection.ADOPTER_TO_ANIMAL)

        assert result.is_disqualified
        assert result.score == 0
        assert result.disqualification_reason
        assert not result.is_recommended


class TestSoftPreferences:
    """Soft preferences reduce the score but never disqualify."""

    def test_species_mismatch_reduces_but_does_not_disqualify(self) -> None:
        """Proves the hard/soft distinction spec section 8 requires."""
        adopter = make_adopter(preferred_species=frozenset({Species.CAT}))
        animal = make_animal(species=Species.DOG)

        result = calculate_match_score(adopter, animal, MatchDirection.ADOPTER_TO_ANIMAL)

        assert not result.is_disqualified
        assert result.score < 100

    def test_wanted_species_scores_above_unwanted(self) -> None:
        """Proves stated preference actually moves the score."""
        animal = make_animal(species=Species.DOG)
        wanted = score_species_preference(
            make_adopter(preferred_species=frozenset({Species.DOG})), animal
        )
        unwanted = score_species_preference(
            make_adopter(preferred_species=frozenset({Species.CAT})), animal
        )

        assert wanted.score > unwanted.score


# -------------------------------------------------------------- criteria


class TestIndividualCriteria:
    """Each criterion is correct on its own."""

    def test_sufficient_time_scores_full(self) -> None:
        """Proves meeting the time requirement scores 100."""
        result = score_daily_availability(
            make_adopter(daily_hours_available=6.0),
            make_animal(activity_level=ActivityLevel.HIGH),
        )
        assert result.score == 100

    def test_insufficient_time_scales_with_the_shortfall(self) -> None:
        """Proves the penalty is proportional, not a flat cliff."""
        animal = make_animal(activity_level=ActivityLevel.HIGH)  # needs 4.0 hours

        half = score_daily_availability(make_adopter(daily_hours_available=2.0), animal)
        quarter = score_daily_availability(make_adopter(daily_hours_available=1.0), animal)

        assert half.score == 50
        assert quarter.score == 25
        assert quarter.score < half.score

    def test_special_needs_animal_needs_experience(self) -> None:
        """Proves a beginner scores lower on an animal requiring experience."""
        animal = make_animal(has_special_needs=True)

        beginner = score_experience_level(
            make_adopter(experience_level=ExperienceLevel.NONE), animal
        )
        expert = score_experience_level(
            make_adopter(experience_level=ExperienceLevel.EXPERIENCED), animal
        )

        assert expert.score > beginner.score

    def test_every_criterion_supplies_an_explanation(self) -> None:
        """Proves no score reaches the interface as a bare number (spec 9.7)."""
        result = calculate_match_score(
            make_adopter(), make_animal(), MatchDirection.ADOPTER_TO_ANIMAL
        )

        assert len(result.criterion_scores) == len(Criterion)
        for criterion_score in result.criterion_scores:
            assert criterion_score.explanation.strip()

    def test_weighted_contributions_sum_to_the_total(self) -> None:
        """Proves the published breakdown actually adds up to the score shown."""
        result = calculate_match_score(
            make_adopter(), make_animal(), MatchDirection.ADOPTER_TO_ANIMAL
        )

        recomputed = sum(item.weighted_contribution for item in result.criterion_scores)

        assert round(recomputed) == result.score


# --------------------------------------------------------------- ranking


class TestRanking:
    """Ranking is ordered, stable and excludes disqualified candidates."""

    def test_animals_are_ranked_best_first(self) -> None:
        """Proves Find My Pet returns descending scores."""
        adopter = make_adopter(
            home_type=HomeType.APARTMENT,
            has_yard=False,
            daily_hours_available=1.5,
            activity_level=ActivityLevel.LOW,
            preferred_species=frozenset({Species.CAT}),
        )
        animals = {
            "calm-cat": make_animal(
                species=Species.CAT, size=AnimalSize.SMALL, required_space=AnimalSize.SMALL,
                activity_level=ActivityLevel.LOW,
            ),
            "busy-dog": make_animal(
                species=Species.DOG, size=AnimalSize.MEDIUM, required_space=AnimalSize.MEDIUM,
                activity_level=ActivityLevel.HIGH,
            ),
        }

        ranked = rank_animals_for_adopter(adopter, animals)

        assert [identifier for identifier, _ in ranked] == ["calm-cat", "busy-dog"]
        assert ranked[0][1].score > ranked[1][1].score

    def test_disqualified_candidates_are_excluded(self) -> None:
        """Proves hard-constraint failures never appear in a ranking."""
        animal = make_animal(good_with_other_animals=False)
        adopters = {
            "has-pets": make_adopter(has_other_animals=True),
            "no-pets": make_adopter(has_other_animals=False),
        }

        ranked = rank_adopters_for_animal(animal, adopters)

        assert [identifier for identifier, _ in ranked] == ["no-pets"]

    def test_ties_break_deterministically(self) -> None:
        """Proves repeated ranking over identical data gives the same order."""
        animal = make_animal()
        adopters = {f"adopter-{index}": make_adopter() for index in range(5)}

        first = [identifier for identifier, _ in rank_adopters_for_animal(animal, adopters)]
        second = [identifier for identifier, _ in rank_adopters_for_animal(animal, adopters)]

        assert first == second == sorted(first)

    def test_empty_candidate_set_returns_empty_ranking(self) -> None:
        """Proves the no-candidates case returns cleanly rather than raising."""
        assert rank_animals_for_adopter(make_adopter(), {}) == []
        assert rank_adopters_for_animal(make_animal(), {}) == []
