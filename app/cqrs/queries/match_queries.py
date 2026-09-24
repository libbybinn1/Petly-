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
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

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
    AnalysisJobStatus,
    AnimalStatus,
    ApplicationStatus,
    MatchDirection,
    Species,
)
from app.domain.facts import adopter_facts_from_row, animal_facts_from_row
from app.domain.matching import (
    AdopterFacts,
    AnimalFacts,
    MatchScore,
    ScoreBand,
    band_for,
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


# Schemes a stored reference may be linked with. An agent-written string is
# not a trusted URL: rendering "javascript:..." or "data:text/html,..." as an
# href would make the record's own evidence an injection vector, and a
# relative-looking value would point at this application instead of at the
# source it names.
LINKABLE_SCHEMES = ("http", "https")


@dataclass(frozen=True)
class EvidenceSource:
    """One source the agent recorded, shaped for display (rule R4).

    A view model rather than the raw dictionary, because the screen needs a
    decision made about the reference - whether it may be a link - and a
    template must not make it.
    """

    kind: str
    reference: str
    cited: bool

    @property
    def safe_reference_url(self) -> str | None:
        """The reference as a link, or None when it must be shown as text.

        Only an absolute http or https URL is linkable. A RAG citation is a
        knowledge-base path such as "space-and-housing.md#apartments", which
        is a reference and not an address, so it renders as text.
        """
        parsed = urlparse(self.reference)
        if parsed.scheme not in LINKABLE_SCHEMES or not parsed.netloc:
            return None
        return self.reference


def _to_evidence_sources(rows: list[Any]) -> list[EvidenceSource]:
    """Convert the stored JSON list into evidence view models.

    A row that is not a dictionary is dropped: the column is NVARCHAR(MAX)
    and what comes back has been through another process, so it is untrusted
    shape as much as untrusted content.
    """
    return [
        EvidenceSource(
            kind=str(row.get("kind", "")),
            reference=str(row.get("reference", "")),
            cited=bool(row.get("cited")),
        )
        for row in rows
        if isinstance(row, dict)
    ]


# How many criteria each compact card shows. The staff card is narrower
# than the adopter's, which is the whole reason the two differ.
TOP_CRITERIA_ON_A_CANDIDATE = 3
TOP_CRITERIA_ON_AN_ANIMAL_CARD = 4


@dataclass(frozen=True)
class CriterionSummary:
    """One criterion on a compact card: its name, its score and its band."""

    name: str
    score: int

    @property
    def band(self) -> ScoreBand:
        """How this criterion reads at a glance (spec section 8)."""
        return band_for(self.score)


def _heaviest_criteria(score: MatchScore, count: int) -> list[CriterionSummary]:
    """The `count` heaviest criteria of a score, for a compact summary.

    Args:
        score: The computed score, with its full breakdown.
        count: How many to return.

    Returns:
        Criterion summaries, heaviest weight first.
    """
    heaviest_first = sorted(score.criterion_scores, key=lambda item: -item.weight)
    return [
        CriterionSummary(
            name=item.criterion.value.replace("_", " ").title(), score=item.score
        )
        for item in heaviest_first[:count]
    ]


@dataclass(frozen=True)
class StoredCriterion:
    """One criterion row as the agent stored it, shaped for the shared partial.

    The live scorer hands the same screens a `CriterionScore`; this is the
    replayed equivalent, and it answers `band` the same way so one partial
    can render either (spec section 8).
    """

    criterion: str
    score: int
    weight: float
    explanation: str

    @property
    def band(self) -> ScoreBand:
        """How this criterion's score reads at a glance."""
        return band_for(self.score)


def _to_stored_criteria(rows: list[Any]) -> list[StoredCriterion]:
    """Convert the stored JSON breakdown into criterion view models."""
    return [
        StoredCriterion(
            criterion=str(row.get("criterion", "")),
            score=int(row.get("score", 0)),
            weight=float(row.get("weight", 0.0)),
            explanation=str(row.get("explanation", "")),
        )
        for row in rows
        if isinstance(row, dict)
    ]


@dataclass(frozen=True)
class StoredAnalysis:
    """An analysis the agent has already produced."""

    match_analysis_id: str
    reasons: list[str]
    concerns: list[str]
    missing_information: list[str]
    evidence_sources: list[EvidenceSource]
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
    def cited_sources(self) -> list[EvidenceSource]:
        """The sources a reason actually referred to.

        Rule R4 requires every claim to trace to something retrieved. An
        uncited source was fetched and not used, which is worth keeping in
        the record but is not evidence for anything on screen.
        """
        return [source for source in self.evidence_sources if source.cited]


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
    def top_criteria(self) -> list[CriterionSummary]:
        """The three heaviest criteria, for the compact summary on the card."""
        return _heaviest_criteria(self.score, TOP_CRITERIA_ON_A_CANDIDATE)


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

    @property
    def top_criteria(self) -> list[CriterionSummary]:
        """The four heaviest criteria, for the compact summary on the card.

        Exposed here rather than left to the template, which used to slice
        `score.criterion_scores[:4]` itself - a view deciding which part of
        the model matters (rule R2), and one that only worked because the
        weights happen to be declared heaviest first.
        """
        return _heaviest_criteria(self.score, TOP_CRITERIA_ON_AN_ANIMAL_CARD)


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

        applicant_ids = [application.adopter_profile_id for application in applications]
        profiles = _profiles_by_id(session, applicant_ids)
        identities = _identities_by_profile(session, applicant_ids)

        candidates: list[RankedCandidate] = []
        excluded = 0

        for application in applications:
            profile = profiles.get(application.adopter_profile_id)
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
                    profile,
                    score,
                    analyses.get(profile.adopter_profile_id),
                    identities.get(profile.adopter_profile_id, UNKNOWN_ADOPTER),
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

        identities = _identities_by_profile(
            session, [profile.adopter_profile_id for profile in eligible_profiles]
        )

        candidates: list[RankedCandidate] = []
        excluded: list[ExcludedCandidate] = []

        for profile in eligible_profiles:
            identity = identities.get(profile.adopter_profile_id, UNKNOWN_ADOPTER)
            score = calculate_match_score(
                _adopter_facts(profile), animal_facts, MatchDirection.ANIMAL_TO_ADOPTER
            )
            if score.is_disqualified:
                # Unlike applicant ranking, discovery keeps these out of the
                # list: staff did not ask about this person, so a
                # disqualified suggestion would be noise in the ranking. The
                # reason is still recorded, so the screen can show what was
                # ruled out and why rather than only a count.
                excluded.append(_to_excluded_candidate(profile, score, identity))
                continue

            candidates.append(
                _build_candidate(
                    profile,
                    score,
                    analyses.get(profile.adopter_profile_id),
                    identity,
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
    criterion_scores: list[StoredCriterion]
    reasons: list[str]
    concerns: list[str]
    missing_information: list[str]
    evidence_sources: list[EvidenceSource]
    used_web_search: bool
    model_name: str
    generated_at: str
    # The ordered steps the agent took. Empty for an analysis written before
    # the agent recorded one, which is why the template guards on it.
    reasoning_trace: list[dict[str, Any]] = field(default_factory=list)

    @property
    def cited_sources(self) -> list[EvidenceSource]:
        """The sources a reason actually referred to.

        Matches `StoredAnalysis.cited_sources`: an uncited source was
        fetched and not used, which belongs in the record but is not
        evidence for anything on screen.
        """
        return [source for source in self.evidence_sources if source.cited]


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
            criterion_scores=_to_stored_criteria(_decode_list(analysis.criterion_scores)),
            reasons=_decode_list(analysis.reasons),
            concerns=_decode_list(analysis.concerns),
            missing_information=_decode_list(analysis.missing_information),
            evidence_sources=_to_evidence_sources(_decode_list(analysis.evidence_sources)),
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
    profile: AdopterProfile, score: MatchScore, identity: AdopterIdentity
) -> ExcludedCandidate:
    """Record one adopter discovery ruled out, with the rule that ruled them out."""
    return ExcludedCandidate(
        adopter_profile_id=profile.adopter_profile_id,
        full_name=identity.full_name,
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


@dataclass(frozen=True)
class AdopterIdentity:
    """The name and email one ranked row displays.

    Fetched for the whole screen in one query rather than per candidate. The
    row lookups used to be `session.get(User, ...)` inside the loop, which
    cost 25 SELECTs to rank 20 adopters against a shared cloud database, and
    the discovery screen paid for every candidate before slicing the list
    down to ten.
    """

    full_name: str
    email: str


# What a ranked row shows when the account behind a profile has vanished. The
# profile is still scorable, so dropping the candidate would hide a real
# applicant over a missing display name.
UNKNOWN_ADOPTER = AdopterIdentity(full_name="Unknown", email="")


def _identities_by_profile(
    session: Session, adopter_profile_ids: Sequence[str]
) -> dict[str, AdopterIdentity]:
    """Fetch every candidate's name and email in one query.

    Args:
        session: A read-only session.
        adopter_profile_ids: Whose names are needed.

    Returns:
        The identity of each profile whose account still exists, keyed by
        adopter profile identifier.
    """
    if not adopter_profile_ids:
        return {}

    rows = session.execute(
        select(AdopterProfile.adopter_profile_id, User.full_name, User.email)
        .join(User, User.user_id == AdopterProfile.user_id)
        .where(AdopterProfile.adopter_profile_id.in_(adopter_profile_ids))
    ).tuples().all()
    return {
        adopter_profile_id: AdopterIdentity(full_name=full_name, email=email)
        for adopter_profile_id, full_name, email in rows
    }


def _profiles_by_id(
    session: Session, adopter_profile_ids: Sequence[str]
) -> dict[str, AdopterProfile]:
    """Fetch several adopter profiles in one query, keyed by identifier.

    Args:
        session: A read-only session.
        adopter_profile_ids: Which profiles to load.

    Returns:
        The profiles that exist, keyed by identifier.
    """
    if not adopter_profile_ids:
        return {}

    rows = (
        session.execute(
            select(AdopterProfile).where(
                AdopterProfile.adopter_profile_id.in_(adopter_profile_ids)
            )
        )
        .scalars()
        .all()
    )
    return {row.adopter_profile_id: row for row in rows}


def _build_candidate(
    profile: AdopterProfile,
    score: MatchScore,
    analysis: StoredAnalysis | None,
    identity: AdopterIdentity,
    application: AdoptionApplication | None = None,
    queued_at_label: str | None = None,
) -> RankedCandidate:
    """Assemble one ranked candidate row."""
    return RankedCandidate(
        adopter_profile_id=profile.adopter_profile_id,
        full_name=identity.full_name,
        email=identity.email,
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
        evidence_sources=_to_evidence_sources(_decode_list(row.evidence_sources)),
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
    """Convert a stored profile into the domain value object (spec section 8).

    Thin by design. The conversion rules live in `app.domain.facts`, which the
    agent and the seed scripts call too: three private copies of this mapping
    once drifted far enough that the agent recomputed 95 where this tier had
    shown 91, because two of them never read the adopter's age preference.
    """
    return adopter_facts_from_row(profile)


def _animal_facts(animal: Animal) -> AnimalFacts:
    """Convert a stored animal into the domain value object (spec section 8)."""
    return animal_facts_from_row(animal)
