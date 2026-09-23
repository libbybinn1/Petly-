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
from app.domain.application_rules import ALLOWED_APPLICATION_TRANSITIONS
from app.domain.enums import (
    ActivityLevel,
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
    AnimalFacts,
    MatchScore,
    calculate_match_score,
)
from app.infrastructure.models import (
    AdopterProfile,
    AdoptionApplication,
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
    evidence_sources: list[dict[str, str]]
    used_web_search: bool
    model_name: str

    @property
    def was_generated_by_a_model(self) -> bool:
        """Whether a language model wrote this, or the deterministic fallback did."""
        return self.model_name != "deterministic-fallback"


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

    @property
    def species_label(self) -> str:
        """Species formatted for display."""
        return self.species.replace("_", " ").title()

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
class RankingResult:
    """A ranked candidate list plus the context a screen needs."""

    animal_id: str
    animal_name: str
    candidates: list[RankedCandidate] = field(default_factory=list)
    excluded_count: int = 0


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

        candidates: list[RankedCandidate] = []
        disqualified = 0

        for profile in eligible_profiles:
            score = calculate_match_score(
                _adopter_facts(profile), animal_facts, MatchDirection.ANIMAL_TO_ADOPTER
            )
            if score.is_disqualified:
                # Unlike applicant ranking, discovery hides these entirely:
                # staff did not ask about this person, so surfacing a
                # disqualified suggestion is noise.
                disqualified += 1
                continue

            candidates.append(
                _build_candidate(
                    session, profile, score, analyses.get(profile.adopter_profile_id)
                )
            )

        return RankingResult(
            animal_id=animal.animal_id,
            animal_name=animal.name,
            candidates=_sorted_candidates(candidates)[: query.limit],
            excluded_count=disqualified,
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

        animals = (
            session.execute(
                select(Animal)
                .options(selectinload(Animal.images))
                .where(Animal.status == AnimalStatus.AVAILABLE.value)
            )
            .scalars()
            .all()
        )

        adopter_facts = _adopter_facts(profile)
        analyses = _analyses_for_adopter(
            session, query.adopter_profile_id, MatchDirection.ADOPTER_TO_ANIMAL
        )

        ranked: list[RankedAnimal] = []
        for animal in animals:
            score = calculate_match_score(
                adopter_facts, _animal_facts(animal), MatchDirection.ADOPTER_TO_ANIMAL
            )
            if score.is_disqualified:
                continue

            ranked.append(
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
                )
            )

        ranked.sort(key=lambda item: (-item.score.score, item.animal_id))
        return ranked[: query.limit]


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
    evidence_sources: list[dict[str, str]]
    used_web_search: bool
    model_name: str
    generated_at: str


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
            generated_at=analysis.generated_at.strftime("%d %b %Y, %H:%M"),
        )


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def _sorted_candidates(candidates: list[RankedCandidate]) -> list[RankedCandidate]:
    """Order candidates best first, breaking ties stably on identifier."""
    return sorted(
        candidates, key=lambda item: (-item.score.score, item.adopter_profile_id)
    )


def _build_candidate(
    session: Session,
    profile: AdopterProfile,
    score: MatchScore,
    analysis: StoredAnalysis | None,
    application: AdoptionApplication | None = None,
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
        open_to_proactive_suggestions=bool(profile.open_to_proactive_suggestions),
        is_complete=bool(profile.is_complete),
    )


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
