"""The fit grade: a match score read as a report card (spec section 8).

A bare "76" says how good a pairing is but not why it is not 100. This module
turns the deterministic score into a grade - a letter and a verdict - and an
itemised account of every point the pairing lost, criterion by criterion,
each with the criterion's own explanation.

The account is exact rather than illustrative. A criterion scored `s` out of
100 at weight `w` costs the pairing `(100 - s) * w` points, and because the
weights sum to one those losses sum to `100 - total`. Rounded with the
largest-remainder method they are whole numbers that add up to precisely
`100 - score`, so the screen can say "every missing point is accounted for"
and mean it.

Pure arithmetic over the scorer's output: no framework, no model. The grade
is never generated text (rule R3), and the agent receives it as a fact to
explain, not as something to decide (rule R4).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.domain.matching import (
    PERFECT_SCORE,
    RECOMMENDATION_THRESHOLD,
    STRONG_MATCH_THRESHOLD,
    ScoreBand,
    band_for,
)

# A deduction smaller than this, after rounding, is not worth a line: it is
# arithmetic noise from a criterion that scored in the high nineties.
MINIMUM_POINTS_WORTH_LISTING = 1


class FitGrade(StrEnum):
    """The letter a pairing earns.

    The boundaries are nested inside the score bands so the grade and the
    colour of the ring can never disagree: every A is STRONG, every C or D
    is FAIR, and only an F is WEAK.
    """

    A_PLUS = "A+"
    A = "A"
    B = "B"
    C = "C"
    D = "D"
    F = "F"


# Lowest score for each grade, highest first. B starts exactly where a match
# becomes STRONG, and D exactly where it becomes worth recommending.
_GRADE_FLOORS: tuple[tuple[int, FitGrade, str], ...] = (
    (95, FitGrade.A_PLUS, "Ideal fit"),
    (88, FitGrade.A, "Excellent fit"),
    (STRONG_MATCH_THRESHOLD, FitGrade.B, "Strong fit"),
    (62, FitGrade.C, "Good fit"),
    (RECOMMENDATION_THRESHOLD, FitGrade.D, "Possible fit"),
    (0, FitGrade.F, "Poor fit"),
)


class ScoredCriterion(Protocol):
    """Anything that reads like one criterion of a score.

    Both the live scorer's `CriterionScore` and the replayed criterion a
    stored analysis carries satisfy this, so one grading function serves
    the ranking cards and the analysis screen alike.
    """

    @property
    def score(self) -> int:
        """The criterion's own 0-100 score."""
        ...

    @property
    def weight(self) -> float:
        """Its share of the total, between 0 and 1."""
        ...

    @property
    def explanation(self) -> str:
        """Why it scored what it did."""
        ...


@dataclass(frozen=True)
class PointDeduction:
    """Points one criterion cost the pairing, and why."""

    criterion_name: str
    points: int
    criterion_score: int
    explanation: str

    @property
    def band(self) -> ScoreBand:
        """How the criterion itself reads, for colouring its line."""
        return band_for(self.criterion_score)


@dataclass(frozen=True)
class FitReport:
    """A score as a grade, a verdict and an itemised list of deductions."""

    score: int
    grade: FitGrade
    verdict: str
    deductions: tuple[PointDeduction, ...]

    @property
    def points_lost(self) -> int:
        """How far the score is from a perfect match."""
        return PERFECT_SCORE - self.score

    @property
    def is_perfect(self) -> bool:
        """Whether nothing at all was deducted."""
        return self.points_lost == 0

    @property
    def band(self) -> ScoreBand:
        """The score's band, so a template can colour the grade."""
        return band_for(self.score)

    @property
    def grade_modifier(self) -> str:
        """The grade as a CSS-safe suffix: "A+" becomes "a-plus"."""
        return self.grade.value.lower().replace("+", "-plus")


def grade_for(score: int) -> tuple[FitGrade, str]:
    """Place a 0-100 score on the grade scale (spec section 8).

    Args:
        score: A total match score.

    Returns:
        The letter and its verdict, each floor inclusive.
    """
    for floor, grade, verdict in _GRADE_FLOORS:
        if score >= floor:
            return grade, verdict
    return FitGrade.F, "Poor fit"


def build_fit_report(
    score: int, criteria: Iterable[tuple[str, ScoredCriterion]]
) -> FitReport:
    """Grade a score and itemise the points it lost (spec section 8).

    Args:
        score: The rounded total the scorer produced.
        criteria: Each criterion's display name paired with its result.

    Returns:
        The report, deductions largest first, ties by name so the order is
        stable across runs. Deductions add up to exactly `100 - score`.
    """
    grade, verdict = grade_for(score)
    named = list(criteria)
    whole_points = _allocate_whole_points(
        [(PERFECT_SCORE - item.score) * item.weight for _name, item in named],
        PERFECT_SCORE - score,
    )

    deductions = [
        PointDeduction(
            criterion_name=name,
            points=points,
            criterion_score=item.score,
            explanation=item.explanation,
        )
        for (name, item), points in zip(named, whole_points, strict=True)
        if points >= MINIMUM_POINTS_WORTH_LISTING
    ]
    deductions.sort(key=lambda deduction: (-deduction.points, deduction.criterion_name))

    return FitReport(score=score, grade=grade, verdict=verdict, deductions=tuple(deductions))


def _allocate_whole_points(exact_losses: list[float], total_to_allocate: int) -> list[int]:
    """Round fractional losses to integers that sum to a fixed total.

    The largest-remainder method: floor every share, then hand the points
    still missing to the shares with the biggest fractional parts. Ties go
    to the earlier criterion, which is the heavier one by declaration order.

    Args:
        exact_losses: Each criterion's unrounded loss.
        total_to_allocate: What the whole numbers must add up to.

    Returns:
        One non-negative integer per loss, in the same order.
    """
    if not exact_losses or total_to_allocate <= 0:
        return [0] * len(exact_losses)

    floors = [math.floor(loss) for loss in exact_losses]
    remaining = total_to_allocate - sum(floors)
    by_remainder = sorted(
        range(len(exact_losses)),
        key=lambda index: (-(exact_losses[index] - floors[index]), index),
    )

    allocated = list(floors)
    if remaining >= 0:
        for index in by_remainder[:remaining]:
            allocated[index] += 1
        return allocated

    # The total rounded down past the floors: take the surplus back from the
    # smallest remainders, never below zero.
    for index in reversed(by_remainder):
        if remaining == 0:
            break
        if allocated[index] > 0:
            allocated[index] -= 1
            remaining += 1
    return allocated
