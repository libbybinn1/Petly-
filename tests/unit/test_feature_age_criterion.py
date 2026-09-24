"""Unit tests for the age-preference criterion (spec section 8).

Spec section 8 names eleven criteria and the scorer implemented ten. The
two `preferred_*` columns existed on `adopter_profiles` and nothing read
them, so an adopter who wanted a puppy was ranked exactly as if they had
said nothing.

Per rule R3 these never touch an LLM: the matching maths is deterministic
by design, so the same input gives the same output every time.
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
    AGE_DISTANCE_SCORES,
    ANIMAL_TO_ADOPTER_WEIGHTS,
    FAR_OUTSIDE_AGE_BAND_SCORE,
    NEUTRAL_SCORE,
    PERFECT_SCORE,
    AdopterFacts,
    AgePreference,
    AnimalFacts,
    Criterion,
    _score_from_age_distance,
    calculate_match_score,
    score_age_preference,
)

pytestmark = pytest.mark.unit


def adopter(**overrides: object) -> AdopterFacts:
    """An adopter whose only interesting field is the one under test."""
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
        "preferred_species": frozenset(),
        "preferred_age_range": None,
        "preferred_size": None,
        "open_to_proactive_suggestions": True,
        "is_complete": True,
    }
    defaults.update(overrides)
    return AdopterFacts(**defaults)  # type: ignore[arg-type]


def animal(age_years: float = 3.0, **overrides: object) -> AnimalFacts:
    """An animal of a given age, otherwise unremarkable."""
    defaults: dict[str, object] = {
        "species": Species.DOG,
        "age_years": age_years,
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


class TestTheWeightsStillBalance:
    """Adding a criterion must not silently rescale every score."""

    @pytest.mark.parametrize(
        ("name", "weights"),
        [
            ("adopter to animal", ADOPTER_TO_ANIMAL_WEIGHTS),
            ("animal to adopter", ANIMAL_TO_ADOPTER_WEIGHTS),
        ],
    )
    def test_each_direction_sums_to_one(
        self, name: str, weights: dict[Criterion, float]
    ) -> None:
        """Proves the weights are a distribution, not an arbitrary total.

        If they summed to 1.09 every score would quietly inflate, and a
        score of 100 would stop meaning a perfect match.
        """
        assert sum(weights.values()) == pytest.approx(1.0), name

    @pytest.mark.parametrize(
        ("name", "weights"),
        [
            ("adopter to animal", ADOPTER_TO_ANIMAL_WEIGHTS),
            ("animal to adopter", ANIMAL_TO_ADOPTER_WEIGHTS),
        ],
    )
    def test_every_criterion_is_weighted_in_both_directions(
        self, name: str, weights: dict[Criterion, float]
    ) -> None:
        """Proves no criterion is scored and then dropped, or vice versa."""
        assert set(weights) == set(Criterion), name

    def test_age_matters_more_to_the_adopter_than_to_the_animal(self) -> None:
        """Proves the two directions disagree about it, as spec 9.2 requires.

        What the adopter wanted is a real preference from their side, and
        close to irrelevant from the animal's - an eight-year-old dog does
        not need a home that wanted an eight-year-old dog, it needs a home
        that can look after it.
        """
        assert (
            ADOPTER_TO_ANIMAL_WEIGHTS[Criterion.AGE_PREFERENCE]
            > ANIMAL_TO_ADOPTER_WEIGHTS[Criterion.AGE_PREFERENCE]
        )


class TestNoPreference:
    """Silence is not a preference."""

    def test_no_preference_scores_neutral(self) -> None:
        """Proves an adopter who said nothing is neither rewarded nor punished."""
        result = score_age_preference(adopter(), animal(age_years=11.0))

        assert result.score == NEUTRAL_SCORE

    def test_any_is_treated_as_a_stated_openness(self) -> None:
        """Proves choosing "any" is an answer, not a blank.

        Somebody who deliberately ticked "any age" has told us something
        useful, and every animal genuinely does suit them on this
        criterion - so it scores full marks rather than neutral.
        """
        result = score_age_preference(
            adopter(preferred_age_range=AgePreference.ANY), animal(age_years=14.0)
        )

        assert result.score == PERFECT_SCORE


class TestInsideTheBand:
    """An animal in the requested range scores full marks."""

    @pytest.mark.parametrize(
        ("preference", "age"),
        [
            (AgePreference.YOUNG, 0.5),
            (AgePreference.YOUNG, 1.9),
            (AgePreference.ADULT, 2.0),
            (AgePreference.ADULT, 7.9),
            (AgePreference.SENIOR, 8.0),
            (AgePreference.SENIOR, 17.0),
        ],
    )
    def test_an_animal_in_the_band_scores_perfectly(
        self, preference: AgePreference, age: float
    ) -> None:
        """Proves each band covers what it says, including its boundaries."""
        result = score_age_preference(
            adopter(preferred_age_range=preference), animal(age_years=age)
        )

        assert result.score == PERFECT_SCORE

    def test_the_bands_tile_the_whole_range(self) -> None:
        """Proves no age falls between two bands.

        A two-year-old must belong to exactly one band, or an adopter
        would see the same animal scored inconsistently.
        """
        for age in (0.0, 1.999, 2.0, 7.999, 8.0, 30.0):
            matches = [
                preference
                for preference in (
                    AgePreference.YOUNG, AgePreference.ADULT, AgePreference.SENIOR
                )
                if score_age_preference(
                    adopter(preferred_age_range=preference), animal(age_years=age)
                ).score == PERFECT_SCORE
            ]
            assert len(matches) == 1, f"age {age} matched {matches}"


class TestOutsideTheBand:
    """Missing the band reduces the score without ruling the animal out."""

    def test_a_near_miss_scores_better_than_a_distant_one(self) -> None:
        """Proves the criterion grades rather than using a cliff edge.

        An adopter wanting 2-8 years is better served by a one-year-old
        than by a fifteen-year-old, and one flat "outside the band" score
        would call those the same.
        """
        near = score_age_preference(
            adopter(preferred_age_range=AgePreference.ADULT), animal(age_years=1.5)
        )
        far = score_age_preference(
            adopter(preferred_age_range=AgePreference.ADULT), animal(age_years=15.0)
        )

        assert near.score > far.score

    def test_being_outside_the_band_is_never_a_disqualification(self) -> None:
        """Proves an age mismatch is soft, as a preference should be.

        Hiding an older animal from somebody who asked for a puppy would
        deny them a choice they might well make when they meet it.
        """
        score = calculate_match_score(
            adopter(preferred_age_range=AgePreference.YOUNG),
            animal(age_years=16.0),
            MatchDirection.ADOPTER_TO_ANIMAL,
        )

        assert not score.is_disqualified
        assert score.score > 0

    def test_a_mismatch_lowers_the_total(self) -> None:
        """Proves the criterion actually affects the ranking."""
        wanted = calculate_match_score(
            adopter(preferred_age_range=AgePreference.YOUNG),
            animal(age_years=1.0),
            MatchDirection.ADOPTER_TO_ANIMAL,
        )
        unwanted = calculate_match_score(
            adopter(preferred_age_range=AgePreference.YOUNG),
            animal(age_years=16.0),
            MatchDirection.ADOPTER_TO_ANIMAL,
        )

        assert wanted.score > unwanted.score


class TestTheExplanation:
    """The reason shown to a person has to be true and readable."""

    def test_the_explanation_names_the_requested_range(self) -> None:
        """Proves the reason says what was asked for, not just that it missed."""
        result = score_age_preference(
            adopter(preferred_age_range=AgePreference.SENIOR), animal(age_years=1.0)
        )

        assert AgePreference.SENIOR.value in result.explanation

    def test_a_young_animal_is_described_in_months(self) -> None:
        """Proves an explanation never reads "0 years old"."""
        result = score_age_preference(
            adopter(preferred_age_range=AgePreference.SENIOR), animal(age_years=0.5)
        )

        assert "month" in result.explanation


class TestTheDistanceBandBoundaries:
    """`AGE_DISTANCE_SCORES` grades the miss; each band edge is a decision.

    An animal a year older than the adopter asked for and one a decade older
    are both outside the band, and scoring them the same would make the
    criterion a cliff edge. The thresholds are inclusive at the top, which is
    only visible at the boundary.
    """

    @pytest.mark.parametrize(
        ("years_outside", "expected"),
        [
            (0.5, 75),
            (1.0, 75),
            (1.01, 55),
            (3.0, 55),
            (3.01, 35),
            (6.0, 35),
            (6.01, FAR_OUTSIDE_AGE_BAND_SCORE),
            (40.0, FAR_OUTSIDE_AGE_BAND_SCORE),
        ],
    )
    def test_each_band_edge_falls_on_the_stated_side(
        self, years_outside: float, expected: int
    ) -> None:
        """Proves every threshold in the table is inclusive at its upper bound."""
        assert _score_from_age_distance(years_outside) == expected

    def test_the_table_is_ordered_nearest_first(self) -> None:
        """Proves the lookup can stop at the first band a distance fits.

        `_score_from_age_distance` returns on the first match, so an
        unordered table would silently score a near miss as a far one.
        """
        limits = [limit for limit, _ in AGE_DISTANCE_SCORES]
        scores = [score for _, score in AGE_DISTANCE_SCORES]

        assert limits == sorted(limits)
        assert scores == sorted(scores, reverse=True)
        assert scores[-1] > FAR_OUTSIDE_AGE_BAND_SCORE

    def test_a_miss_never_scores_as_well_as_a_hit(self) -> None:
        """Proves the graded miss is still a miss.

        Negative half of the pair: the point of grading is to rank misses
        against each other, not to make one indistinguishable from an animal
        inside the requested band.
        """
        for _, score in AGE_DISTANCE_SCORES:
            assert score < PERFECT_SCORE
