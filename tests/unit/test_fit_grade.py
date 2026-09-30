"""Unit tests for the fit grade and its itemised deductions (spec section 8).

No database, no Flask, no model: the grade is arithmetic over the scorer's
output, so it must be checkable in isolation and give identical output for
identical input (rule R3).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

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
from app.domain.fit_grade import (
    FitGrade,
    FitReport,
    ScoredCriterion,
    build_fit_report,
    grade_for,
)
from app.domain.matching import (
    PERFECT_SCORE,
    RECOMMENDATION_THRESHOLD,
    STRONG_MATCH_THRESHOLD,
    MatchScore,
    ScoreBand,
    band_for,
    calculate_match_score,
)

from tests.unit.test_matching import make_adopter, make_animal

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class Criterion:
    """A stand-in criterion result, shaped like the scorer's."""

    score: int
    weight: float
    explanation: str = "Because."


def report_for_live_score(
    adopter_overrides: dict[str, object], animal_overrides: dict[str, object]
) -> tuple[MatchScore, FitReport]:
    """Score a pairing with the real scorer and grade it."""
    score = calculate_match_score(
        make_adopter(**adopter_overrides),
        make_animal(**animal_overrides),
        MatchDirection.ADOPTER_TO_ANIMAL,
    )
    named: list[tuple[str, ScoredCriterion]] = [
        (item.criterion.value, item) for item in score.criterion_scores
    ]
    return score, build_fit_report(score.score, named)


# A spread of households and animals, so the sum property is checked
# against many different mixes of fractional losses rather than one.
HOUSEHOLDS: list[dict[str, object]] = [
    {},
    {"home_type": HomeType.APARTMENT, "has_yard": False, "daily_hours_available": 1.0},
    {"experience_level": ExperienceLevel.NONE, "activity_level": ActivityLevel.LOW},
    {"household_has_children": True, "youngest_child_age": 8, "has_other_animals": True},
    {"preferred_species": frozenset({Species.CAT}), "city": "Eilat"},
]
ANIMALS: list[dict[str, object]] = [
    {},
    {"activity_level": ActivityLevel.HIGH, "temperament": Temperament.ENERGETIC},
    {"size": AnimalSize.SMALL, "required_space": AnimalSize.SMALL, "species": Species.CAT},
    {"has_special_needs": True, "age_years": 11.0},
    {"good_with_other_animals": False, "city": "Tel Aviv"},
]


class TestEveryPointIsAccountedFor:
    """The deductions are exact, not illustrative."""

    @pytest.mark.parametrize(
        ("household", "animal"), list(itertools.product(HOUSEHOLDS, ANIMALS))
    )
    def test_deductions_add_up_to_exactly_what_the_score_is_missing(
        self, household: dict[str, object], animal: dict[str, object]
    ) -> None:
        """Proves "every missing point, itemised" holds for real scorer output.

        Weights are fractional, so each criterion's true loss is too. The
        largest-remainder rounding is what lets whole-number deductions sum
        to precisely 100 minus the rounded score.
        """
        score, report = report_for_live_score(household, animal)
        if score.is_disqualified:
            pytest.skip("a disqualified pairing has no breakdown to itemise")

        assert sum(deduction.points for deduction in report.deductions) == (
            PERFECT_SCORE - score.score
        )

    def test_the_largest_deduction_comes_first(self) -> None:
        """Proves the list reads as "what cost the most", top down."""
        report = build_fit_report(
            70,
            [
                ("small", Criterion(score=90, weight=0.5)),
                ("large", Criterion(score=50, weight=0.5)),
            ],
        )

        assert [deduction.criterion_name for deduction in report.deductions] == [
            "large",
            "small",
        ]
        assert [deduction.points for deduction in report.deductions] == [25, 5]

    def test_each_deduction_carries_its_criterion_explanation(self) -> None:
        """Proves the "why" is the scorer's own sentence, not new text."""
        report = build_fit_report(
            80, [("daily availability", Criterion(40, 1 / 3, "Needs 4 hours; has 1."))]
        )

        assert report.deductions[0].explanation == "Needs 4 hours; has 1."

    def test_identical_input_gives_identical_output(self) -> None:
        """Proves the report is deterministic, ties included (rule R3)."""
        criteria: list[tuple[str, ScoredCriterion]] = [
            ("b", Criterion(80, 0.25)),
            ("a", Criterion(80, 0.25)),
            ("c", Criterion(60, 0.5)),
        ]

        assert build_fit_report(75, criteria) == build_fit_report(75, list(criteria))


class TestNegativeAndEdgeCases:
    """Blueprint section 17: failure scenarios, not only the happy path."""

    def test_a_perfect_score_has_nothing_to_itemise(self) -> None:
        """Proves a flawless pairing shows no invented deductions."""
        report = build_fit_report(100, [("all", Criterion(100, 1.0))])

        assert report.is_perfect
        assert report.deductions == ()
        assert report.grade is FitGrade.A_PLUS

    def test_a_criterion_that_lost_nothing_is_not_listed(self) -> None:
        """Proves a full-marks criterion never appears as a zero-point line."""
        report = build_fit_report(
            90, [("fine", Criterion(100, 0.5)), ("short", Criterion(80, 0.5))]
        )

        assert [deduction.criterion_name for deduction in report.deductions] == ["short"]

    def test_no_criteria_yields_no_deductions_rather_than_an_error(self) -> None:
        """Proves an empty breakdown, as an old stored analysis has, degrades safely."""
        report = build_fit_report(60, [])

        assert report.deductions == ()
        assert report.points_lost == 40

    def test_no_deduction_is_ever_negative(self) -> None:
        """Proves rounding a total up cannot produce a negative line.

        A total like 87.6 rounds to 88, leaving fewer whole points to hand
        out than the floors imply only if the method were wrong.
        """
        report = build_fit_report(
            88,
            [
                ("a", Criterion(87, 0.4)),
                ("b", Criterion(88, 0.3)),
                ("c", Criterion(88, 0.3)),
            ],
        )

        assert all(deduction.points > 0 for deduction in report.deductions)
        assert sum(deduction.points for deduction in report.deductions) == 12


class TestTheGradeScale:
    """The letter agrees with the band everywhere."""

    @pytest.mark.parametrize("score", range(0, 101))
    def test_the_grade_never_contradicts_the_band(self, score: int) -> None:
        """Proves no score can be both an A and coloured weak, say.

        The ring's colour comes from `band_for`; the letter from `grade_for`.
        A and A+ and B must be STRONG, C and D FAIR, and only F WEAK.
        """
        grade, _verdict = grade_for(score)
        expected_band = {
            FitGrade.A_PLUS: ScoreBand.STRONG,
            FitGrade.A: ScoreBand.STRONG,
            FitGrade.B: ScoreBand.STRONG,
            FitGrade.C: ScoreBand.FAIR,
            FitGrade.D: ScoreBand.FAIR,
            FitGrade.F: ScoreBand.WEAK,
        }[grade]

        assert band_for(score) is expected_band

    def test_the_thresholds_are_the_grade_boundaries(self) -> None:
        """Proves B starts at "strong" and D at "worth recommending"."""
        assert grade_for(STRONG_MATCH_THRESHOLD)[0] is FitGrade.B
        assert grade_for(STRONG_MATCH_THRESHOLD - 1)[0] is FitGrade.C
        assert grade_for(RECOMMENDATION_THRESHOLD)[0] is FitGrade.D
        assert grade_for(RECOMMENDATION_THRESHOLD - 1)[0] is FitGrade.F

    def test_a_grade_has_a_css_safe_modifier(self) -> None:
        """Proves "A+" becomes a class name a stylesheet can target."""
        report = build_fit_report(97, [("x", Criterion(97, 1.0))])

        assert report.grade_modifier == "a-plus"
