"""Read-side queries for matching: rankings, discovery and stored analyses.

Two things worth knowing about how these work.

**Ranking is synchronous and deterministic.** Every handler here computes
scores in pure Python and returns immediately. No language model is called,
because CPU-only inference takes 11-16 seconds and ranking N candidates that
way would take minutes (NFR-3.1).

**Explanations are asynchronous.** The agent writes a `MatchAnalysis` some
seconds later. These queries join whatever analysis already exists onto the
freshly-computed ranking, so a screen shows correct scores instantly and
fills in the prose as it arrives.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.cqrs.base import Query, QueryHandler
from app.cqrs.queries.animal_queries import (
    DEFAULT_PAGE_SIZE,
    AnimalCard,
    AnimalSearchFilters,
    apply_search_filters,
    to_animal_card,
)
from app.cqrs.queries.formatting import describe_relative_time, to_display_moment
from app.domain.application_rules import ALLOWED_APPLICATION_TRANSITIONS
from app.domain.enums import (
    ActivityLevel,
    AnalysisJobStatus,
    AnimalSize,
    AnimalStatus,
    ApplicationStatus,
    ExperienceLevel,
    HomeType,
    MatchDirection,
    Species,
    Temperament,
)
from app.domain.matching import (
    AdopterFacts,
    AgePreference,
    AnimalFacts,
    MatchScore,
    calculate_match_score,
)
from app.infrastructure.models import (
    AdopterProfile,
    AdoptionApplication,
    AnalysisJob,
    Animal,
    MatchAnalysis,
    User,
)
from app.infrastructure.sql_helpers import is_true

DEFAULT_DISCOVERY_LIMIT = 10


@dataclass(frozen=True)
class StoredAnalysis:
    """An analysis the agent has already produced."""

    match_analysis_id: str
    reasons: list[str]
    concerns: list[str]
    missing_information: list[str]
    # Passed through as whatever the agent stored. The values are not all
    # strings - each source carries a boolean saying whether a reason
    # actually cited it - and rebuilding these dicts key by key would drop
    # any field the agent adds later.
    evidence_sources: list[dict[str, Any]]
    used_web_search: bool
    model_name: str
    # The ordered steps the agent took, each a dict of step, action and
    # detail. Empty for an analysis written before the agent recorded one,
    # which is why the template guards on it.
    reasoning_trace: list[dict[str, Any]] = field(default_factory=list)

    @property
    def was_generated_by_a_model(self) -> bool:
        """Whether a language model wrote this, or the deterministic fallback did."""
        return self.model_name != "deterministic-fallback"

    @property
    def cited_sources(self) -> list[dict[str, Any]]:
        """The sources a reason actually referred to.

        Rule R4 requires every claim to trace to something retrieved. An
        uncited source was fetched and not used, which is worth keeping in
        the record but is not evidence for anything on screen.
        """
        return [source for source in self.evidence_sources if source.get("cited")]


@dataclass(frozen=True)
class RankedCandidate:
    """One adopter ranked against one animal, for the staff screens."""

    adopter_profile_id: str
    full_name: str
    email: str
    city: str
    home_type: str
    experience_level: str
    daily_hours_available: float
    has_children: bool
    has_other_animals: bool
    score: MatchScore
    application_id: str | None = None
    application_status: str | None = None
    analysis: StoredAnalysis | None = None
    # "4 minutes ago", when a job for this pairing is still queued. None
    # when nothing is queued, which is a different state from waiting: the
    # explanation may simply never have been asked for.
    queued_at_label: str | None = None

    @property
    def analysis_is_pending(self) -> bool:
        """Whether the agent has not yet written an explanation.

        The score is already correct; only the prose is outstanding.
        """
        return self.analysis is None

    @property
    def current_status(self) -> ApplicationStatus | None:
        """This candidate's application status, if they applied at all."""
        if self.application_status is None:
            return None
        try:
            return ApplicationStatus(self.application_status)
        except ValueError:
            return None

    @property
    def can_approve(self) -> bool:
        """Whether an Approve control should be offered for this candidate.

        The template asks rather than deciding: which transitions are legal
        belongs in the domain, and the same table the handler enforces is
        what answers here - so a button can never offer something the
        command would refuse.
        """
        status = self.current_status
        return status is not None and ApplicationStatus.APPROVED in (
            ALLOWED_APPLICATION_TRANSITIONS.get(status, frozenset())
        )

    @property
    def can_reject(self) -> bool:
        """Whether a Reject control should be offered."""
        status = self.current_status
        return status is not None and ApplicationStatus.REJECTED in (
            ALLOWED_APPLICATION_TRANSITIONS.get(status, frozenset())
        )

    @property
    def can_mark_under_review(self) -> bool:
        """Whether a "start reviewing" control should be offered."""
        status = self.current_status
        return status is not None and ApplicationStatus.UNDER_REVIEW in (
            ALLOWED_APPLICATION_TRANSITIONS.get(status, frozenset())
        )

    @property
    def can_reverse(self) -> bool:
        """Whether this approval can be undone.

        Only an approved application, matching `ensure_approval_may_be_reversed`.
        """
        return self.current_status is ApplicationStatus.APPROVED

    @property
    def status_label(self) -> str:
        """The application status in prose, or a note that they did not apply."""
        status = self.current_status
        if status is None:
            return "Has not applied"
        return status.value.replace("_", " ").capitalize()

    @property
    def top_criteria(self) -> list[tuple[str, int]]:
        """The three heaviest criteria and their scores, for a compact summary."""
        ranked = sorted(self.score.criterion_scores, key=lambda item: -item.weight)
        return [
            (item.criterion.value.replace("_", " ").title(), item.score)
            for item in ranked[:3]
        ]


@dataclass(frozen=True)
class RankedAnimal:
    """One animal ranked for an adopter, for Find My Pet."""

    animal_id: str
    name: str
    species: str
    breed: str | None
    age_years: float
    city: str
    image_url: str | None
    score: MatchScore
    analysis: StoredAnalysis | None = None
    # See RankedCandidate.queued_at_label.
    queued_at_label: str | None = None

    @property
    def species_label(self) -> str:
        """Species formatted for display.

        Matches `AnimalCard.species_label`: OTHER is a bucket rather than
        a kind of animal, so the breed carries the real description.
        """
        if self.species == Species.OTHER.value and self.breed:
            return self.breed
        return self.species.replace("_", " ").capitalize()

    @property
    def breed_note(self) -> str | None:
        """The breed, unless it is already doing duty as the species label.

        Templates show "species · breed". For an OTHER-species animal the
        label *is* the breed, so without this the card would read
        "Ferret · Ferret".
        """
        if self.breed and self.breed != self.species_label:
            return self.breed
        return None

    @property
    def analysis_is_pending(self) -> bool:
        """Whether the agent has not yet written an explanation."""
        return self.analysis is None


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RankApplicantsQuery(Query):
    """Rank the adopters who already applied for one animal (Find My Adopter)."""

    animal_id: str


@dataclass(frozen=True)
class FindMoreAdoptersQuery(Query):
    """Discover eligible adopters who did *not* apply (Find More Adopters)."""

    animal_id: str
    limit: int = DEFAULT_DISCOVERY_LIMIT


@dataclass(frozen=True)
class FindMyPetQuery(Query):
    """Rank available animals for one adopter (Find My Pet)."""

    adopter_profile_id: str
    limit: int = DEFAULT_DISCOVERY_LIMIT


@dataclass(frozen=True)
class FindMyPetWithIntentQuery(Query):
    """Rank animals for one adopter, narrowed by what they just asked for.

    Spec section 6.4 calls the combination of a stable profile with a current
    intent "the key distinction" of this product: the profile says what suits
    this household, the description says what they are in the mood for today,
    and neither alone is the answer.

    The narrowing is a database filter and the ranking is the deterministic
    scorer. No model is involved at this point - the model's only
    contribution was turning prose into `filters`, and that happened in
    another process before this query ran (rule R4).

    Attributes:
        adopter_profile_id: Whose profile ranks the results.
        filters: The interpreted criteria, from `filters_from_intent`.
        limit: How many animals to return.
    """

    adopter_profile_id: str
    filters: AnimalSearchFilters
    limit: int = DEFAULT_PAGE_SIZE


@dataclass(frozen=True)
class ExcludedCandidate:
    """An adopter discovery rejected, and the rule that rejected them.

    Spec section 10 runs eligibility *before* ranking, so these people never
    reach the list. Reporting only how many were excluded told a staff member
    that a judgement had been made and nothing about what it was, which is
    the one thing they would want to check.
    """

    adopter_profile_id: str
    full_name: str
    disqualification_reason: str


@dataclass(frozen=True)
class RankingResult:
    """A ranked candidate list plus the context a screen needs."""

    animal_id: str
    animal_name: str
    candidates: list[RankedCandidate] = field(default_factory=list)
    excluded_count: int = 0
    # Populated by discovery, which hides disqualified candidates entirely.
    # Applicant ranking leaves it empty because it shows its disqualified
    # applicants in the list itself, flagged - staff asked about those
    # people by name, so hiding them would be the wrong answer.
    excluded_candidates: list[ExcludedCandidate] = field(default_factory=list)


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------


class RankApplicantsHandler(QueryHandler[RankingResult | None]):
    """Answers RankApplicantsQuery."""

    def handle(self, query: Query, session: Session) -> RankingResult | None:
        """Rank existing applicants for an animal, best first."""
        assert isinstance(query, RankApplicantsQuery)

        animal = session.get(Animal, query.animal_id)
        if animal is None:
            return None

        active_statuses = [status.value for status in ApplicationStatus if status.is_active]
        applications = (
            session.execute(
                select(AdoptionApplication)
                .where(AdoptionApplication.animal_id == query.animal_id)
                .where(AdoptionApplication.status.in_(active_statuses))
            )
            .scalars()
            .all()
        )

        animal_facts = _animal_facts(animal)
        analyses = _analyses_for_animal(
            session, query.animal_id, MatchDirection.ANIMAL_TO_ADOPTER
        )
        queued_labels = _queued_labels_for_animal(session, query.animal_id)

        candidates: list[RankedCandidate] = []
        excluded = 0

        for application in applications:
            profile = session.get(AdopterProfile, application.adopter_profile_id)
            if profile is None:
                continue

            score = calculate_match_score(
                _adopter_facts(profile), animal_facts, MatchDirection.ANIMAL_TO_ADOPTER
            )
            if score.is_disqualified:
                # Still shown to staff, but flagged - a disqualified applicant
                # is information, not something to hide.
                excluded += 1

            candidates.append(
                _build_candidate(
                    session,
                    profile,
                    score,
                    analyses.get(profile.adopter_profile_id),
                    application,
                    queued_labels.get(profile.adopter_profile_id),
                )
            )

        return RankingResult(
            animal_id=animal.animal_id,
            animal_name=animal.name,
            candidates=_sorted_candidates(candidates),
            excluded_count=excluded,
        )


class FindMoreAdoptersHandler(QueryHandler[RankingResult | None]):
    """Answers FindMoreAdoptersQuery.

    Deliberately different from ranking applicants: this discovers people who
    did **not** apply, and only those who satisfy every eligibility rule in
    spec section 10.
    """

    def handle(self, query: Query, session: Session) -> RankingResult | None:
        """Rank eligible opted-in adopters who have not applied."""
        assert isinstance(query, FindMoreAdoptersQuery)

        animal = session.get(Animal, query.animal_id)
        if animal is None:
            return None

        if AnimalStatus(animal.status) is not AnimalStatus.AVAILABLE:
            # Eligibility rule: the animal must be available. Returning an
            # empty list is honest; ranking candidates for an animal nobody
            # can adopt would waste staff attention.
            return RankingResult(animal.animal_id, animal.name, [], 0)

        already_applied = {
            row.adopter_profile_id
            for row in session.execute(
                select(AdoptionApplication).where(
                    AdoptionApplication.animal_id == query.animal_id
                )
            )
            .scalars()
            .all()
        }

        eligible_profiles = self._eligible_profiles(session, already_applied)
        animal_facts = _animal_facts(animal)
        analyses = _analyses_for_animal(
            session, query.animal_id, MatchDirection.ANIMAL_TO_ADOPTER
        )
        queued_labels = _queued_labels_for_animal(session, query.animal_id)

        candidates: list[RankedCandidate] = []
        excluded: list[ExcludedCandidate] = []

        for profile in eligible_profiles:
            score = calculate_match_score(
                _adopter_facts(profile), animal_facts, MatchDirection.ANIMAL_TO_ADOPTER
            )
            if score.is_disqualified:
                # Unlike applicant ranking, discovery keeps these out of the
                # list: staff did not ask about this person, so a
                # disqualified suggestion would be noise in the ranking. The
                # reason is still recorded, so the screen can show what was
                # ruled out and why rather than only a count.
                excluded.append(_to_excluded_candidate(session, profile, score))
                continue

            candidates.append(
                _build_candidate(
                    session,
                    profile,
                    score,
                    analyses.get(profile.adopter_profile_id),
                    None,
                    queued_labels.get(profile.adopter_profile_id),
                )
            )

        return RankingResult(
            animal_id=animal.animal_id,
            animal_name=animal.name,
            candidates=_sorted_candidates(candidates)[: query.limit],
            excluded_count=len(excluded),
            excluded_candidates=sorted(excluded, key=lambda item: item.full_name),
        )

    @staticmethod
    def _eligible_profiles(
        session: Session, already_applied: set[str]
    ) -> list[AdopterProfile]:
        """Apply the deterministic eligibility rules from spec section 10.

        Run *before* any ranking, so obviously unsuitable candidates never
        reach the scoring stage at all.
        """
        rows = (
            session.execute(
                select(AdopterProfile)
                .join(User, User.user_id == AdopterProfile.user_id)
                .where(is_true(AdopterProfile.is_complete))
                .where(is_true(AdopterProfile.open_to_proactive_suggestions))
                .where(is_true(User.is_active))
            )
            .scalars()
            .all()
        )
        return [row for row in rows if row.adopter_profile_id not in already_applied]


class FindMyPetHandler(QueryHandler[list[RankedAnimal]]):
    """Answers FindMyPetQuery."""

    def handle(self, query: Query, session: Session) -> list[RankedAnimal]:
        """Rank available animals for one adopter, best first."""
        assert isinstance(query, FindMyPetQuery)

        profile = session.get(AdopterProfile, query.adopter_profile_id)
        if profile is None:
            return []

        scored = _score_available_animals(session, profile, filters=None)
        analyses = _analyses_for_adopter(
            session, query.adopter_profile_id, MatchDirection.ADOPTER_TO_ANIMAL
        )
        queued_labels = _queued_labels_for_adopter(session, query.adopter_profile_id)

        ranked = [
            RankedAnimal(
                animal_id=animal.animal_id,
                name=animal.name,
                species=animal.species,
                breed=animal.breed,
                age_years=float(animal.age_years),
                city=animal.city,
                image_url=animal.primary_image_url,
                score=score,
                analysis=analyses.get(animal.animal_id),
                queued_at_label=queued_labels.get(animal.animal_id),
            )
            for animal, score in scored
        ]
        return ranked[: query.limit]


class FindMyPetWithIntentHandler(QueryHandler[list[AnimalCard]]):
    """Answers FindMyPetWithIntentQuery (spec section 6.4).

    Returns cards rather than the richer `RankedAnimal` because this feeds a
    results grid: the same `_animal_card.html` the ordinary search renders,
    with a score on it. The alternative - a second card layout for the one
    screen that fuses a profile with an intent - is exactly the drift
    docs/UX.md section 3 forbids.
    """

    def handle(self, query: Query, session: Session) -> list[AnimalCard]:
        """Rank the animals matching an intent against one adopter's profile.

        An adopter whose profile has been deleted between the job being
        queued and this page being opened gets an empty list rather than an
        error: their description has already been interpreted, and the
        intent-only results are one link away.
        """
        assert isinstance(query, FindMyPetWithIntentQuery)

        profile = session.get(AdopterProfile, query.adopter_profile_id)
        if profile is None:
            return []

        scored = _score_available_animals(session, profile, query.filters)
        return [
            to_animal_card(animal, match_score=score.score)
            for animal, score in scored[: query.limit]
        ]


@dataclass(frozen=True)
class GetMatchAnalysisQuery(Query):
    """Fetch one stored analysis with its full breakdown."""

    match_analysis_id: str


@dataclass(frozen=True)
class MatchAnalysisDetail:
    """Everything the analysis detail screen shows."""

    match_analysis_id: str
    animal_name: str
    animal_id: str
    adopter_name: str
    direction: str
    score: int
    is_disqualified: bool
    criterion_scores: list[dict[str, object]]
    reasons: list[str]
    concerns: list[str]
    missing_information: list[str]
    # Passed through as the agent stored them. Not `dict[str, str]`: each
    # source carries a `cited` boolean saying whether a reason actually
    # referred to it, and the template shows that marker (rule R4).
    evidence_sources: list[dict[str, Any]]
    used_web_search: bool
    model_name: str
    generated_at: str
    # The ordered steps the agent took. Empty for an analysis written before
    # the agent recorded one, which is why the template guards on it.
    reasoning_trace: list[dict[str, Any]] = field(default_factory=list)

    @property
    def cited_sources(self) -> list[dict[str, Any]]:
        """The sources a reason actually referred to.

        Matches `StoredAnalysis.cited_sources`: an uncited source was
        fetched and not used, which belongs in the record but is not
        evidence for anything on screen.
        """
        return [source for source in self.evidence_sources if source.get("cited")]


class GetMatchAnalysisHandler(QueryHandler[MatchAnalysisDetail | None]):
    """Answers GetMatchAnalysisQuery."""

    def handle(self, query: Query, session: Session) -> MatchAnalysisDetail | None:
        """Return one analysis, or None when it does not exist."""
        assert isinstance(query, GetMatchAnalysisQuery)

        analysis = session.get(MatchAnalysis, query.match_analysis_id)
        if analysis is None:
            return None

        animal = session.get(Animal, analysis.animal_id)
        profile = session.get(AdopterProfile, analysis.adopter_profile_id)
        user = session.get(User, profile.user_id) if profile else None

        return MatchAnalysisDetail(
            match_analysis_id=analysis.match_analysis_id,
            animal_name=animal.name if animal else "Unknown animal",
            animal_id=analysis.animal_id,
            adopter_name=user.full_name if user else "Unknown adopter",
            direction=analysis.direction,
            score=analysis.score,
            is_disqualified=bool(analysis.is_disqualified),
            criterion_scores=_decode_list(analysis.criterion_scores),
            reasons=_decode_list(analysis.reasons),
            concerns=_decode_list(analysis.concerns),
            missing_information=_decode_list(analysis.missing_information),
            evidence_sources=_decode_list(analysis.evidence_sources),
            used_web_search=bool(analysis.used_web_search),
            model_name=analysis.model_name,
            generated_at=to_display_moment(analysis.generated_at).display,
            reasoning_trace=_decode_list(analysis.reasoning_trace),
        )


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def _sorted_candidates(candidates: list[RankedCandidate]) -> list[RankedCandidate]:
    """Order candidates best first, breaking ties stably on identifier."""
    return sorted(
        candidates, key=lambda item: (-item.score.score, item.adopter_profile_id)
    )


def _score_available_animals(
    session: Session, profile: AdopterProfile, filters: AnimalSearchFilters | None
) -> list[tuple[Animal, MatchScore]]:
    """Score every available animal for one adopter, best first.

    Shared by Find My Pet and its profile-fused variant, which differ only
    in what they narrow to and what they build from the result. Keeping the
    scoring in one place is what guarantees the same animal cannot hold two
    different scores on two screens for the same adopter (FR-9.7).

    Disqualified pairings are dropped rather than ranked last: a hard
    constraint is a rule, not a low score (spec section 8), and offering an
    adopter an animal they may not adopt would be a worse answer than a
    shorter list.

    Args:
        session: A read-only session.
        profile: The adopter to score against.
        filters: Extra narrowing, from an interpreted intent. None ranks
            every available animal.

    Returns:
        (animal, score) pairs, highest score first, ties broken on identifier.
    """
    statement = select(Animal).options(selectinload(Animal.images))
    if filters is None:
        statement = statement.where(Animal.status == AnimalStatus.AVAILABLE.value)
    else:
        statement = apply_search_filters(statement, filters)

    adopter_facts = _adopter_facts(profile)
    scored: list[tuple[Animal, MatchScore]] = []

    for animal in session.execute(statement).scalars().all():
        score = calculate_match_score(
            adopter_facts, _animal_facts(animal), MatchDirection.ADOPTER_TO_ANIMAL
        )
        if not score.is_disqualified:
            scored.append((animal, score))

    scored.sort(key=lambda pair: (-pair[1].score, pair[0].animal_id))
    return scored


def _to_excluded_candidate(
    session: Session, profile: AdopterProfile, score: MatchScore
) -> ExcludedCandidate:
    """Record one adopter discovery ruled out, with the rule that ruled them out."""
    user = session.get(User, profile.user_id)
    return ExcludedCandidate(
        adopter_profile_id=profile.adopter_profile_id,
        full_name=user.full_name if user else "Unknown",
        disqualification_reason=(
            score.disqualification_reason or "A hard eligibility rule was not met."
        ),
    )


def _queued_labels_for_animal(session: Session, animal_id: str) -> dict[str, str]:
    """How long each outstanding job for this animal has been waiting.

    Fetched in one query for the whole screen rather than per candidate, for
    the same reason as `_analyses_for_animal`: ranking twenty applicants
    should not issue twenty round trips to a shared cloud database.

    Args:
        session: A read-only session.
        animal_id: The animal being ranked for.

    Returns:
        A relative label such as "4 minutes ago", keyed by adopter profile.
        Adopters with nothing queued are absent.
    """
    rows = session.execute(
        select(AnalysisJob.adopter_profile_id, AnalysisJob.created_at)
        .where(AnalysisJob.animal_id == animal_id)
        .where(AnalysisJob.status.in_(_OUTSTANDING_JOB_STATUSES))
        .order_by(AnalysisJob.created_at)
    ).tuples().all()
    return {
        adopter_profile_id: describe_relative_time(created_at)
        for adopter_profile_id, created_at in rows
        if adopter_profile_id is not None
    }


def _queued_labels_for_adopter(
    session: Session, adopter_profile_id: str
) -> dict[str, str]:
    """How long each outstanding job for this adopter has been waiting.

    Args:
        session: A read-only session.
        adopter_profile_id: The adopter being ranked for.

    Returns:
        A relative label such as "4 minutes ago", keyed by animal. Animals
        with nothing queued are absent.
    """
    rows = session.execute(
        select(AnalysisJob.animal_id, AnalysisJob.created_at)
        .where(AnalysisJob.adopter_profile_id == adopter_profile_id)
        .where(AnalysisJob.status.in_(_OUTSTANDING_JOB_STATUSES))
        .order_by(AnalysisJob.created_at)
    ).tuples().all()
    return {
        animal_id: describe_relative_time(created_at)
        for animal_id, created_at in rows
        if animal_id is not None
    }


# A job the page is still waiting on. IN_PROGRESS counts because the wait is
# the same from the screen's point of view.
_OUTSTANDING_JOB_STATUSES = (
    AnalysisJobStatus.PENDING.value,
    AnalysisJobStatus.IN_PROGRESS.value,
)


def _build_candidate(
    session: Session,
    profile: AdopterProfile,
    score: MatchScore,
    analysis: StoredAnalysis | None,
    application: AdoptionApplication | None = None,
    queued_at_label: str | None = None,
) -> RankedCandidate:
    """Assemble one ranked candidate row."""
    user = session.get(User, profile.user_id)
    return RankedCandidate(
        adopter_profile_id=profile.adopter_profile_id,
        full_name=user.full_name if user else "Unknown",
        email=user.email if user else "",
        city=profile.city,
        home_type=profile.home_type,
        experience_level=profile.experience_level,
        daily_hours_available=float(profile.daily_hours_available),
        has_children=bool(profile.household_has_children),
        has_other_animals=bool(profile.has_other_animals),
        score=score,
        application_id=application.application_id if application else None,
        application_status=application.status if application else None,
        analysis=analysis,
        queued_at_label=queued_at_label,
    )


def _analyses_for_animal(
    session: Session, animal_id: str, direction: MatchDirection
) -> dict[str, StoredAnalysis]:
    """Stored analyses for one animal in one direction, keyed by adopter.

    Fetched in one query rather than per candidate: ranking twenty applicants
    should not issue twenty round trips to a shared cloud database.

    Filtering on direction matters more than it looks. The same pair is
    scored from both sides with different weightings, so an unfiltered
    lookup returned whichever row was written last - and the explanation on
    screen would then belong to a different number than the score beside it
    (FR-9.7). `ix_analyses_animal_direction` exists for exactly this filter.

    Args:
        session: A read-only session.
        animal_id: The animal being ranked for.
        direction: Which side's weighting the caller is displaying.

    Returns:
        The most recent matching analysis per adopter.
    """
    rows = (
        session.execute(
            select(MatchAnalysis)
            .where(
                MatchAnalysis.animal_id == animal_id,
                MatchAnalysis.direction == direction.value,
            )
            .order_by(MatchAnalysis.generated_at)
        )
        .scalars()
        .all()
    )
    # Later rows overwrite earlier ones, leaving the most recent per adopter.
    return {row.adopter_profile_id: _to_stored_analysis(row) for row in rows}


def _analyses_for_adopter(
    session: Session, adopter_profile_id: str, direction: MatchDirection
) -> dict[str, StoredAnalysis]:
    """Stored analyses for one adopter in one direction, keyed by animal.

    Args:
        session: A read-only session.
        adopter_profile_id: The adopter being ranked for.
        direction: Which side's weighting the caller is displaying.

    Returns:
        The most recent matching analysis per animal.
    """
    rows = (
        session.execute(
            select(MatchAnalysis)
            .where(
                MatchAnalysis.adopter_profile_id == adopter_profile_id,
                MatchAnalysis.direction == direction.value,
            )
            .order_by(MatchAnalysis.generated_at)
        )
        .scalars()
        .all()
    )
    return {row.animal_id: _to_stored_analysis(row) for row in rows}


def _to_stored_analysis(row: MatchAnalysis) -> StoredAnalysis:
    """Convert a stored row into its view model."""
    return StoredAnalysis(
        match_analysis_id=row.match_analysis_id,
        reasons=_decode_list(row.reasons),
        concerns=_decode_list(row.concerns),
        missing_information=_decode_list(row.missing_information),
        evidence_sources=_decode_list(row.evidence_sources),
        used_web_search=bool(row.used_web_search),
        model_name=row.model_name,
        reasoning_trace=_decode_list(row.reasoning_trace),
    )


def _decode_list(raw_json: str | None) -> list[Any]:
    """Decode a JSON column, tolerating malformed content.

    These are NVARCHAR(MAX) rather than a JSON type, because SQL Server 2014
    has none. A corrupt value should blank one panel, not break the page.
    """
    if not raw_json:
        return []
    try:
        decoded = json.loads(raw_json)
    except json.JSONDecodeError:
        return []
    return decoded if isinstance(decoded, list) else []


def _adopter_facts(profile: AdopterProfile) -> AdopterFacts:
    """Convert a stored profile into the domain value object."""
    return AdopterFacts(
        home_type=_enum_or(HomeType, profile.home_type, HomeType.APARTMENT),
        has_yard=bool(profile.has_yard),
        household_has_children=bool(profile.household_has_children),
        youngest_child_age=profile.youngest_child_age,
        has_other_animals=bool(profile.has_other_animals),
        experience_level=_enum_or(
            ExperienceLevel, profile.experience_level, ExperienceLevel.NONE
        ),
        activity_level=_enum_or(
            ActivityLevel, profile.activity_level, ActivityLevel.MODERATE
        ),
        daily_hours_available=float(profile.daily_hours_available or 0),
        city=profile.city or "",
        preferred_species=_stored_species(profile.preferred_species),
        preferred_age_range=_stored_enum(AgePreference, profile.preferred_age_range),
        preferred_size=_stored_enum(AnimalSize, profile.preferred_size),
        open_to_proactive_suggestions=bool(profile.open_to_proactive_suggestions),
        is_complete=bool(profile.is_complete),
    )


def _stored_enum(enum_class: type[EnumT], raw_value: str | None) -> EnumT | None:
    """Read one optional stored preference, dropping anything unrecognised.

    Same reasoning as `_stored_species`: a value the enum no longer
    defines is unknown, not a preference, and inventing one from it would
    quietly narrow somebody's matches.
    """
    if not raw_value:
        return None
    try:
        return enum_class(raw_value.strip())
    except ValueError:
        return None


def _stored_species(raw_column: str | None) -> frozenset[Species]:
    """Read a stored species preference list, dropping anything unrecognised.

    An unrecognised value must not become `Species.OTHER`. OTHER is a real
    preference, not a marker for "unknown", and species preference carries
    the heaviest weight when ranking animals for an adopter - so a typo or
    a retired value in this column would have recorded the adopter as
    actively wanting animals of no listed species, and scored every such
    animal 100 on that criterion. Dropping the value records what is
    actually known about it, which is nothing.

    Args:
        raw_column: The comma-separated column, which may be null.

    Returns:
        The species that parsed. An unparseable one is left out.
    """
    parsed: list[Species] = []
    for value in (raw_column or "").split(","):
        if not value.strip():
            continue
        try:
            parsed.append(Species(value.strip()))
        except ValueError:
            continue
    return frozenset(parsed)


def _animal_facts(animal: Animal) -> AnimalFacts:
    """Convert a stored animal into the domain value object."""
    return AnimalFacts(
        species=_enum_or(Species, animal.species, Species.OTHER),
        age_years=float(animal.age_years or 0),
        size=_enum_or(AnimalSize, animal.size, AnimalSize.MEDIUM),
        temperament=_enum_or(Temperament, animal.temperament, Temperament.BALANCED),
        activity_level=_enum_or(
            ActivityLevel, animal.activity_level, ActivityLevel.MODERATE
        ),
        good_with_children=bool(animal.good_with_children),
        good_with_other_animals=bool(animal.good_with_other_animals),
        has_special_needs=bool(animal.has_special_needs),
        required_space=_enum_or(AnimalSize, animal.required_space, AnimalSize.MEDIUM),
        city=animal.city or "",
    )


EnumT = TypeVar("EnumT", bound=Enum)


def _enum_or(enum_class: type[EnumT], raw_value: object, default: EnumT) -> EnumT:
    """Convert a stored string to an enum member, falling back on a bad value.

    Generic over the enum so a decoded `home_type` is typed as a `HomeType`
    and not as a bare object - which is what lets the type checker verify
    that the fact objects below are assembled from the right enums.

    Args:
        enum_class: The enum the stored string should name.
        raw_value: The stored value.
        default: Used when the stored value names no member.

    Returns:
        The matching member, or the default.
    """
    try:
        return enum_class(str(raw_value))
    except ValueError:
        return default
