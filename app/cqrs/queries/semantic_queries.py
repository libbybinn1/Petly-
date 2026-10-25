"""Read side of semantic adopter search (lecturer requirement 6).

Staff queue a `SEMANTIC_ADOPTER_SEARCH` job from the discovery screen; the
agent embeds the animal, searches the adopter résumé index and stores the
ranked matches as JSON on the job row. This module reads that answer back.
The payload is decoded through `app.domain.semantic_profiles`, the shared
vocabulary both processes import, so nothing here imports agent code
(rule R2).

**Similarity never replaces the deterministic verdict.** Every semantic
match is scored here by the same `calculate_match_score` discovery uses, and
checked against the same eligibility rules (spec section 10), so the screen
shows "close in meaning" and "allowed by the rules" side by side. A match
the rules exclude is shown flagged, never hidden and never invitable.

Reads only: no write, no commit, no model call.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cqrs.base import Query, QueryHandler
from app.cqrs.queries.formatting import DisplayDate, to_display_moment
from app.domain.enums import AnalysisJobStatus, AnalysisJobType, AnimalStatus, MatchDirection
from app.domain.facts import adopter_facts_from_row, animal_facts_from_row
from app.domain.matching import AnimalFacts, ScoreBand, band_for, calculate_match_score
from app.domain.semantic_profiles import (
    SemanticAdopterMatch,
    SemanticSearchOutcome,
    payload_to_semantic_search,
)
from app.infrastructure.models import AdopterProfile, AdoptionApplication, AnalysisJob, Animal, User

# The reasons a semantic match is not invitable, in the staff member's terms.
# Each mirrors a rule the deterministic discovery screen applies (spec 10).
REASON_PROFILE_MISSING = "This adopter profile no longer exists."
REASON_ANIMAL_UNAVAILABLE = "The animal is not available for adoption."
REASON_ALREADY_APPLIED = "Already applied for this animal; see the applicant ranking."
REASON_NOT_DISCOVERABLE = (
    "Not open to proactive suggestions, or the profile is incomplete or inactive."
)

PERCENT = 100

SCORE_TONE_BY_BAND = {
    ScoreBand.STRONG: "success",
    ScoreBand.FAIR: "warning",
    ScoreBand.WEAK: "brand",
}


@dataclass(frozen=True)
class SemanticMatchRow:
    """One semantic match, beside its deterministic verdict.

    Attributes:
        adopter_profile_id: The adopter.
        full_name: The adopter's display name, or "Unknown".
        city: Where they live, empty when the profile is gone.
        similarity: Cosine similarity of résumé and animal text, in [-1, 1].
        deterministic_score: The rule-based score, or None when a rule
            other than scoring excluded them first.
        ineligibility_reason: Why the rules exclude this adopter, or None
            when they are eligible and may be invited.
    """

    adopter_profile_id: str
    full_name: str
    city: str
    similarity: float
    deterministic_score: int | None = None
    ineligibility_reason: str | None = None

    @property
    def is_eligible(self) -> bool:
        """Whether the deterministic rules allow inviting this adopter."""
        return self.ineligibility_reason is None

    @property
    def similarity_percent(self) -> int:
        """Similarity as a 0-100 figure for a bar; negatives show as zero."""
        return round(max(self.similarity, 0.0) * PERCENT)

    @property
    def score_tone(self) -> str:
        """The tag modifier for the deterministic score's band.

        Decided here rather than in the template, which would otherwise be
        comparing bands - a template that compares is a template deciding
        (rule R2). The band itself is the domain's `band_for`.
        """
        if self.deterministic_score is None:
            return "info"
        return SCORE_TONE_BY_BAND[band_for(self.deterministic_score)]


@dataclass(frozen=True)
class SemanticSearchView:
    """The newest semantic search for one animal, as the discovery screen sees it.

    Attributes:
        analysis_job_id: The job that answers.
        status: Where the agent has got to.
        created_at: When it was queued.
        completed_at: When it finished, if it has.
        matches: Nearest first; present only once the job COMPLETED.
        indexed_profile_count: How many résumés the search ran over.
        embedding_model: Which model made the vectors.
    """

    analysis_job_id: str
    status: AnalysisJobStatus
    created_at: datetime
    completed_at: datetime | None = None
    matches: list[SemanticMatchRow] = field(default_factory=list)
    indexed_profile_count: int = 0
    embedding_model: str = ""

    @property
    def is_waiting(self) -> bool:
        """Whether the agent has not finished; a retried job still counts."""
        return self.status in (AnalysisJobStatus.PENDING, AnalysisJobStatus.IN_PROGRESS)

    @property
    def has_failed(self) -> bool:
        """Whether the agent gave up on this search."""
        return self.status is AnalysisJobStatus.FAILED

    @property
    def is_ready(self) -> bool:
        """Whether there are results to show, even if the list is empty."""
        return self.status is AnalysisJobStatus.COMPLETED

    @property
    def eligible_count(self) -> int:
        """How many matches the deterministic rules would allow inviting."""
        return sum(1 for match in self.matches if match.is_eligible)

    @property
    def queued_at(self) -> DisplayDate:
        """When this was queued, in the forms a screen needs."""
        return to_display_moment(self.created_at)

    @property
    def searched_at(self) -> DisplayDate | None:
        """When the search finished, or None while it has not."""
        if self.completed_at is None:
            return None
        return to_display_moment(self.completed_at)


@dataclass(frozen=True)
class GetSemanticAdopterMatchesQuery(Query):
    """Fetch the newest semantic adopter search for one animal.

    Attributes:
        animal_id: The animal on the discovery screen.
    """

    animal_id: str


class GetSemanticAdopterMatchesHandler(QueryHandler[SemanticSearchView | None]):
    """Answers GetSemanticAdopterMatchesQuery."""

    def handle(self, query: Query, session: Session) -> SemanticSearchView | None:
        """Return the newest search for the animal, with each match's verdict.

        Returns:
            The view model, or None when no semantic search was ever queued
            for this animal, or the animal does not exist.
        """
        assert isinstance(query, GetSemanticAdopterMatchesQuery)

        animal = session.get(Animal, query.animal_id)
        if animal is None:
            return None

        job = session.execute(
            select(AnalysisJob)
            .where(
                AnalysisJob.animal_id == query.animal_id,
                AnalysisJob.job_type == AnalysisJobType.SEMANTIC_ADOPTER_SEARCH.value,
            )
            .order_by(AnalysisJob.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if job is None:
            return None

        status = AnalysisJobStatus(job.status)
        outcome = (
            _decode_outcome(job.result_payload)
            if status is AnalysisJobStatus.COMPLETED
            else SemanticSearchOutcome()
        )
        return SemanticSearchView(
            analysis_job_id=job.analysis_job_id,
            status=status,
            created_at=job.created_at,
            completed_at=job.completed_at,
            matches=_match_rows(session, animal, outcome.matches),
            indexed_profile_count=outcome.indexed_profile_count,
            embedding_model=outcome.embedding_model,
        )


def _decode_outcome(raw_payload: str | None) -> SemanticSearchOutcome:
    """Read the stored outcome, treating an unreadable payload as no matches.

    SQL Server 2014 has no JSON type, so the column is NVARCHAR(MAX) text
    written by another process. A completed job whose text cannot be read
    shows as an empty result rather than as a 500.
    """
    if not raw_payload:
        return SemanticSearchOutcome()
    try:
        decoded = json.loads(raw_payload)
    except json.JSONDecodeError:
        return SemanticSearchOutcome()
    if not isinstance(decoded, dict):
        return SemanticSearchOutcome()
    return payload_to_semantic_search(decoded)


def _match_rows(
    session: Session, animal: Animal, matches: Sequence[SemanticAdopterMatch]
) -> list[SemanticMatchRow]:
    """Attach the deterministic verdict to every semantic match.

    Two queries regardless of how many matches there are - one for the
    profiles with their accounts, one for the applicants - because the
    database is remote (docs/ARCHITECTURE.md section 10).
    """
    if not matches:
        return []

    profile_ids = [match.adopter_profile_id for match in matches]
    profiles = _profiles_with_accounts(session, profile_ids)
    applicants = set(
        session.execute(
            select(AdoptionApplication.adopter_profile_id).where(
                AdoptionApplication.animal_id == animal.animal_id,
                AdoptionApplication.adopter_profile_id.in_(profile_ids),
            )
        ).scalars()
    )
    animal_facts = animal_facts_from_row(animal)
    animal_is_available = AnimalStatus(animal.status) is AnimalStatus.AVAILABLE

    return [
        _match_row(
            match,
            profiles.get(match.adopter_profile_id),
            animal_facts,
            animal_is_available=animal_is_available,
            has_applied=match.adopter_profile_id in applicants,
        )
        for match in matches
    ]


def _profiles_with_accounts(
    session: Session, profile_ids: Sequence[str]
) -> dict[str, tuple[AdopterProfile, User]]:
    """Fetch the matched profiles and their accounts in one query."""
    rows = session.execute(
        select(AdopterProfile, User)
        .join(User, User.user_id == AdopterProfile.user_id)
        .where(AdopterProfile.adopter_profile_id.in_(profile_ids))
    ).tuples()
    return {profile.adopter_profile_id: (profile, user) for profile, user in rows}


def _match_row(
    match: SemanticAdopterMatch,
    profile_and_account: tuple[AdopterProfile, User] | None,
    animal_facts: AnimalFacts,
    *,
    animal_is_available: bool,
    has_applied: bool,
) -> SemanticMatchRow:
    """Build one row, applying the discovery rules in the order they apply.

    The order mirrors discovery: the animal must be available, applicants
    belong to the applicant ranking, only discoverable profiles may be
    suggested, and only then does the scorer's hard-constraint check run.
    """
    if profile_and_account is None:
        return SemanticMatchRow(
            adopter_profile_id=match.adopter_profile_id,
            full_name="Unknown",
            city="",
            similarity=match.similarity,
            ineligibility_reason=REASON_PROFILE_MISSING,
        )

    profile, account = profile_and_account
    score = calculate_match_score(
        adopter_facts_from_row(profile), animal_facts, MatchDirection.ANIMAL_TO_ADOPTER
    )
    reason = _rule_exclusion(
        profile, account, animal_is_available=animal_is_available, has_applied=has_applied
    )
    if reason is None and score.is_disqualified:
        reason = score.disqualification_reason or "Excluded by a hard eligibility rule."

    return SemanticMatchRow(
        adopter_profile_id=profile.adopter_profile_id,
        full_name=account.full_name,
        city=profile.city,
        similarity=match.similarity,
        # A disqualified pairing has no meaningful score: a hard constraint
        # is a rule, not a low number (spec section 8).
        deterministic_score=None if score.is_disqualified else score.score,
        ineligibility_reason=reason,
    )


def _rule_exclusion(
    profile: AdopterProfile,
    account: User,
    *,
    animal_is_available: bool,
    has_applied: bool,
) -> str | None:
    """Return the discovery rule that excludes this adopter, if any (spec 10)."""
    if not animal_is_available:
        return REASON_ANIMAL_UNAVAILABLE
    if has_applied:
        return REASON_ALREADY_APPLIED
    is_discoverable = (
        bool(profile.is_complete)
        and bool(profile.open_to_proactive_suggestions)
        and bool(account.is_active)
    )
    if not is_discoverable:
        return REASON_NOT_DISCOVERABLE
    return None
