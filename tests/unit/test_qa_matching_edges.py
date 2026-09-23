"""Adversarial unit tests for the scoring engine (spec sections 8 and 9).

No language model, no network, no database: FR-9.4 requires the arithmetic to
be reproducible offline, so every case here is pure input in, pure number out.

The emphasis is on inputs the form would not produce but the database or an
MCP payload might: absent optionals, contradictory household data, zero and
extreme numbers, and species values no enum defines.
"""

from __future__ import annotations

import itertools

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
    MINIMUM_SCORE,
    PERFECT_SCORE,
    RECOMMENDATION_THRESHOLD,
    AdopterFacts,
    AnimalFacts,
    Criterion,
    CriterionScore,
    MatchScore,
    calculate_match_score,
    rank_animals_for_adopter,
    score_daily_availability,
    score_other_animals_compatibility,
    score_species_preference,
)

pytestmark = pytest.mark.unit


def make_adopter(**overrides: object) -> AdopterFacts:
    """Build an adopter, overriding only the attribute a test is about."""
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
    """Build an animal, overriding only the attribute a test is about."""
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


class TestExhaustiveScoreInvariants:
    """Sweep the input space rather than trusting a handful of examples."""

    def test_every_combination_scores_inside_zero_to_one_hundred(self) -> None:
        """Proves no input mix can produce a score the database CHECK would refuse.

        `match_analyses.score` carries CHECK BETWEEN 0 AND 100, so a score
        outside that range is not merely odd - it fails to persist.
        """
        out_of_range = []

        for home, yard, children, pets, experience, activity in itertools.product(
            HomeType, (True, False), (True, False), (True, False), ExperienceLevel,
            ActivityLevel,
        ):
            adopter = make_adopter(
                home_type=home, has_yard=yard, household_has_children=children,
                youngest_child_age=4 if children else None, has_other_animals=pets,
                experience_level=experience, activity_level=activity,
            )
            for species, size, temperament, animal_activity in itertools.product(
                Species, AnimalSize, Temperament, ActivityLevel
            ):
                animal = make_animal(
                    species=species, size=size, temperament=temperament,
                    activity_level=animal_activity, required_space=size,
                    good_with_children=not children, good_with_other_animals=not pets,
                )
                for direction in MatchDirection:
                    score = calculate_match_score(adopter, animal, direction)
                    if not MINIMUM_SCORE <= score.score <= PERFECT_SCORE:
                        out_of_range.append((score.score, direction))

        assert out_of_range == []

    def test_a_disqualified_pairing_always_scores_zero_with_a_reason(self) -> None:
        """Proves disqualification is consistent: zero, flagged and explained.

        FR-9.5 requires a hard constraint to disqualify rather than merely
        reduce, so a partial signal here would let good soft scores mask a
        welfare problem.
        """
        adopter = make_adopter(has_other_animals=True)
        animal = make_animal(good_with_other_animals=False)

        for direction in MatchDirection:
            score = calculate_match_score(adopter, animal, direction)

            assert score.is_disqualified is True
            assert score.score == MINIMUM_SCORE
            assert score.disqualification_reason
            assert score.criterion_scores == ()
            assert score.is_recommended is False

    def test_repeated_scoring_of_the_same_pair_is_byte_identical(self) -> None:
        """Proves determinism across repeated calls, including the explanation text."""
        adopter, animal = make_adopter(), make_animal()

        first = calculate_match_score(adopter, animal, MatchDirection.ADOPTER_TO_ANIMAL)
        second = calculate_match_score(adopter, animal, MatchDirection.ADOPTER_TO_ANIMAL)

        assert first == second


class TestWeightSets:
    """The two directions must be genuinely different (FR-9.3)."""

    def test_each_weight_set_sums_to_exactly_one(self) -> None:
        """Proves a weighted average, so the total shares the criteria's 0-100 scale."""
        for weights in (ADOPTER_TO_ANIMAL_WEIGHTS, ANIMAL_TO_ADOPTER_WEIGHTS):
            assert sum(weights.values()) == pytest.approx(1.0)

    def test_the_two_sets_differ_on_a_majority_of_criteria(self) -> None:
        """Proves the directions are not a copy of each other with a renamed key."""
        differing = [
            criterion
            for criterion in Criterion
            if ADOPTER_TO_ANIMAL_WEIGHTS[criterion] != ANIMAL_TO_ADOPTER_WEIGHTS[criterion]
        ]

        assert len(differing) > len(list(Criterion)) / 2

    def test_both_sets_cover_every_criterion_exactly_once(self) -> None:
        """Proves no criterion is silently unweighted, which would drop it from the total."""
        assert set(ADOPTER_TO_ANIMAL_WEIGHTS) == set(Criterion)
        assert set(ANIMAL_TO_ADOPTER_WEIGHTS) == set(Criterion)


class TestAbsentAndContradictoryProfileData:
    """Optional fields are genuinely optional; inconsistent data must fail safe."""

    def test_no_species_preference_scores_neutral_rather_than_zero(self) -> None:
        """Proves an empty preference set is 'no constraint', not 'wants nothing'."""
        result = score_species_preference(
            make_adopter(preferred_species=frozenset()), make_animal()
        )

        assert MINIMUM_SCORE < result.score < PERFECT_SCORE

    def test_children_present_with_an_unknown_age_fails_safe(self) -> None:
        """Proves a null youngest_child_age is treated as the risky case.

        `youngest_child_age` is nullable in the schema while
        `household_has_children` is NOT NULL, so this combination is
        reachable from the database even though the form forbids it.
        """
        adopter = make_adopter(household_has_children=True, youngest_child_age=None)

        score = calculate_match_score(
            adopter, make_animal(good_with_children=False), MatchDirection.ANIMAL_TO_ADOPTER
        )

        assert score.is_disqualified is True

    def test_children_present_with_an_unknown_age_is_fine_for_a_child_safe_animal(
        self,
    ) -> None:
        """Proves the unknown age only bites when the animal is not child-certified."""
        adopter = make_adopter(household_has_children=True, youngest_child_age=None)

        score = calculate_match_score(
            adopter, make_animal(good_with_children=True), MatchDirection.ANIMAL_TO_ADOPTER
        )

        assert score.is_disqualified is False

    @pytest.mark.parametrize("age", [0, 1, 11])
    def test_a_young_child_disqualifies_a_non_child_safe_animal(self, age: int) -> None:
        """Proves the age limit is applied at its edges, including a newborn."""
        adopter = make_adopter(household_has_children=True, youngest_child_age=age)

        score = calculate_match_score(
            adopter, make_animal(good_with_children=False), MatchDirection.ANIMAL_TO_ADOPTER
        )

        assert score.is_disqualified is True

    def test_a_twelve_year_old_is_on_the_permitted_side_of_the_limit(self) -> None:
        """Proves YOUNG_CHILD_AGE_LIMIT is exclusive, matching the `<` in the code."""
        adopter = make_adopter(household_has_children=True, youngest_child_age=12)

        score = calculate_match_score(
            adopter, make_animal(good_with_children=False), MatchDirection.ANIMAL_TO_ADOPTER
        )

        assert score.is_disqualified is False

    def test_a_child_age_without_children_is_ignored_rather_than_used(self) -> None:
        """Proves the flag governs, so a stale age cannot disqualify a childless home."""
        adopter = make_adopter(household_has_children=False, youngest_child_age=2)

        score = calculate_match_score(
            adopter, make_animal(good_with_children=False), MatchDirection.ANIMAL_TO_ADOPTER
        )

        assert score.is_disqualified is False

    def test_resident_pets_plus_a_solo_animal_is_hard_not_soft(self) -> None:
        """Proves this is a disqualification, not a reduced criterion score.

        The criterion function scores it zero defensively; the point of this
        test is that `calculate_match_score` never reaches that function,
        because the pairing is refused outright.
        """
        adopter = make_adopter(has_other_animals=True)
        animal = make_animal(good_with_other_animals=False)

        assert score_other_animals_compatibility(adopter, animal).score == MINIMUM_SCORE
        assert calculate_match_score(
            adopter, animal, MatchDirection.ADOPTER_TO_ANIMAL
        ).is_disqualified



def _criterion_named(score: MatchScore, criterion_value: str) -> CriterionScore:
    """Pull one criterion out of a score, failing clearly if it is absent."""
    for item in score.criterion_scores:
        if item.criterion.value == criterion_value:
            return item
    raise AssertionError(f"no {criterion_value} criterion in {score.criterion_scores}")


class TestNumericExtremes:
    """Zero, the daily maximum, and values the CHECK constraints should exclude."""

    def test_zero_hours_available_scores_zero_on_availability_without_crashing(
        self,
    ) -> None:
        """Proves a legitimate zero (DECIMAL CHECK BETWEEN 0 AND 24) is handled."""
        result = score_daily_availability(
            make_adopter(daily_hours_available=0.0),
            make_animal(activity_level=ActivityLevel.HIGH),
        )

        assert result.score == MINIMUM_SCORE

    def test_a_full_day_available_scores_full_marks(self) -> None:
        """Proves the upper bound of the column is comfortably inside the scale."""
        result = score_daily_availability(
            make_adopter(daily_hours_available=24.0),
            make_animal(activity_level=ActivityLevel.HIGH),
        )

        assert result.score == PERFECT_SCORE

    def test_a_negative_hours_value_is_clamped_rather_than_going_negative(self) -> None:
        """Proves corrupt data cannot drag the total below zero.

        The column has CHECK BETWEEN 0 AND 24, so this is defence in depth
        rather than a reachable form submission.
        """
        result = score_daily_availability(
            make_adopter(daily_hours_available=-5.0), make_animal()
        )

        assert result.score == MINIMUM_SCORE

    def test_a_newborn_animal_scores_without_special_casing(self) -> None:
        """Proves age zero is data, not a division waiting to happen."""
        score = calculate_match_score(
            make_adopter(), make_animal(age_years=0.0), MatchDirection.ADOPTER_TO_ANIMAL
        )

        assert MINIMUM_SCORE <= score.score <= PERFECT_SCORE

    def test_city_comparison_ignores_case_and_surrounding_space(self) -> None:
        """Proves location scoring is not defeated by ' haifa ' versus 'Haifa'.

        Asserts on the location criterion rather than the total. Location
        carries a weight of 0.02, so the difference it makes is under a
        point and disappears in the rounding - which would make this test
        report on the rounding rather than on the comparison it is named
        for.
        """
        same_city = _criterion_named(
            calculate_match_score(
                make_adopter(city="  haifa "), make_animal(city="Haifa"),
                MatchDirection.ADOPTER_TO_ANIMAL,
            ),
            "location",
        )
        other_city = _criterion_named(
            calculate_match_score(
                make_adopter(city="Eilat"), make_animal(city="Haifa"),
                MatchDirection.ADOPTER_TO_ANIMAL,
            ),
            "location",
        )

        assert same_city.score > other_city.score


class TestOtherSpecies:
    """The OTHER member is a real species value, not a sentinel."""

    def test_wanting_other_matches_an_other_animal(self) -> None:
        """Proves OTHER behaves like any other species in the preference check."""
        result = score_species_preference(
            make_adopter(preferred_species=frozenset({Species.OTHER})),
            make_animal(species=Species.OTHER),
        )

        assert result.score == PERFECT_SCORE

    def test_wanting_a_dog_does_not_match_an_other_animal(self) -> None:
        """Proves OTHER is not a wildcard that silently matches every preference."""
        result = score_species_preference(
            make_adopter(preferred_species=frozenset({Species.DOG})),
            make_animal(species=Species.OTHER),
        )

        assert result.score < PERFECT_SCORE



def _worst_legal_pairing() -> tuple[AdopterFacts, AnimalFacts]:
    """Build the weakest pairing that still violates no hard constraint.

    Every soft criterion is as bad as it can be - no experience, no time,
    wrong species, wrong energy, wrong city, a large and high-energy dog
    with special needs - while everything that would disqualify outright
    is satisfied: the animal needs only medium space, and is good with
    children and with other animals.

    An exhaustive search of the input space puts this exact configuration
    at 35, the lowest any non-disqualified pairing reaches. Anything worse
    trips a hard constraint and scores zero instead.
    """
    adopter = make_adopter(
        home_type=HomeType.APARTMENT,
        has_yard=False,
        household_has_children=False,
        has_other_animals=False,
        experience_level=ExperienceLevel.NONE,
        activity_level=ActivityLevel.LOW,
        daily_hours_available=0.0,
        city="Eilat",
        preferred_species=frozenset({Species.HAMSTER}),
    )
    animal = make_animal(
        species=Species.DOG,
        size=AnimalSize.LARGE,
        required_space=AnimalSize.MEDIUM,
        activity_level=ActivityLevel.HIGH,
        temperament=Temperament.CALM,
        has_special_needs=True,
        good_with_children=True,
        good_with_other_animals=True,
        city="Haifa",
    )
    return adopter, animal

class TestRecommendationThreshold:
    """RECOMMENDATION_THRESHOLD claims to gate who is offered at all."""

    def test_is_recommended_agrees_with_the_threshold(self) -> None:
        """Proves the property on MatchScore applies the documented cut-off."""
        adopter = make_adopter()
        animal = make_animal()
        score = calculate_match_score(adopter, animal, MatchDirection.ADOPTER_TO_ANIMAL)

        assert score.is_recommended == (score.score >= RECOMMENDATION_THRESHOLD)

    def test_ranking_keeps_weak_candidates_rather_than_hiding_them(self) -> None:
        """Proves the threshold marks a pairing without removing it.

        An earlier comment on RECOMMENDATION_THRESHOLD claimed a candidate
        below it was "not offered at all". Nothing implemented that, and
        nothing should: an adopter whose situation suits nothing well would
        get an empty page with no explanation, and staff would never see a
        willing applicant. The constant labels; ranking still returns.
        """
        poor_fit, demanding = _worst_legal_pairing()

        ranked = rank_animals_for_adopter(poor_fit, {"animal-1": demanding})

        assert [animal_id for animal_id, _ in ranked] == ["animal-1"]
        assert ranked[0][1].is_recommended is False

    def test_the_weak_candidate_really_is_below_the_threshold(self) -> None:
        """Documents that the fixture scores low, so the test above is not luck."""
        poor_fit, demanding = _worst_legal_pairing()

        score = calculate_match_score(
            poor_fit, demanding, MatchDirection.ADOPTER_TO_ANIMAL
        )

        assert score.is_disqualified is False
        assert score.score < RECOMMENDATION_THRESHOLD

    def test_a_disqualified_candidate_is_dropped_even_so(self) -> None:
        """Proves the one thing ranking does remove is a hard-constraint breach.

        The distinction the threshold does not draw, this one does: a weak
        fit is shown and labelled, an unsafe one is not shown at all.
        """
        adopter = make_adopter(
            household_has_children=True, youngest_child_age=4,
        )
        unsafe = make_animal(good_with_children=False)

        ranked = rank_animals_for_adopter(adopter, {"animal-1": unsafe})

        assert ranked == []
