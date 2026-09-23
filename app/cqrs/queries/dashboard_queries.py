"""Read-side queries for the staff dashboard (blueprint 4.4, spec section 22).

Spec section 22 asks for a working operational dashboard rather than a
decorative page, and specifically for a *Needs Attention* section connecting
statistics to actions. So every figure here is either something staff act on
or something that tells them they need not.

Two implementation notes:

- **Recent activity comes from the event log**, not a separate audit table
  (FR-6.4). The log already records who did what and when; a second copy
  would be one more thing to keep in step.
- **The counts are computed in as few round trips as practical.** This is a
  shared cloud database on a free tier, and a dashboard that issued a query
  per tile would be the slowest page in the product.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.cqrs.base import Query, QueryHandler
from app.domain.enums import (
    AnimalStatus,
    ApplicationStatus,
    DomainEventType,
    InvitationStatus,
)
from app.domain.matching import RECOMMENDATION_THRESHOLD
from app.eventstore.store import EventStore
from app.infrastructure.models import (
    AdoptionApplication,
    AdoptionInvitation,
    Animal,
    MatchAnalysis,
    User,
)

RECENT_ACTIVITY_LIMIT = 12

# An application nobody has looked at for this long needs chasing.
STALE_APPLICATION_DAYS = 7

# Human phrasing for each event type, so the activity feed reads as a
# sentence rather than as a class name.
EVENT_DESCRIPTIONS: dict[DomainEventType, str] = {
    DomainEventType.APPLICATION_SUBMITTED: "applied for",
    DomainEventType.APPLICATION_UNDER_REVIEW: "started reviewing an application for",
    DomainEventType.APPLICATION_APPROVED: "approved an adoption of",
    DomainEventType.APPLICATION_REJECTED: "rejected an application for",
    DomainEventType.APPLICATION_WITHDRAWN: "withdrew an application for",
    DomainEventType.APPLICATION_CLOSED_DUE_TO_OTHER_APPROVAL: (
        "had an application closed for"
    ),
    DomainEventType.APPLICATION_REOPENED: "had an application reopened for",
    DomainEventType.INVITATION_SENT: "invited an adopter to meet",
    DomainEventType.INVITATION_VIEWED: "viewed an invitation for",
    DomainEventType.INVITATION_ACCEPTED: "accepted an invitation for",
    DomainEventType.INVITATION_DECLINED: "declined an invitation for",
    DomainEventType.INVITATION_EXPIRED: "let an invitation expire for",
    DomainEventType.AI_ANALYSIS_COMPLETED: "completed a match analysis for",
    DomainEventType.ANIMAL_STATUS_CHANGED: "changed the status of",
    DomainEventType.ADOPTER_PROFILE_UPDATED: "updated their profile",
}


@dataclass(frozen=True)
class ActivityEntry:
    """One line of the recent-activity feed, derived from an event."""

    occurred_at: datetime
    actor_name: str
    description: str
    animal_name: str | None
    animal_id: str | None

    @property
    def sentence(self) -> str:
        """The entry as a readable line."""
        if self.animal_name:
            return f"{self.actor_name} {self.description} {self.animal_name}"
        return f"{self.actor_name} {self.description}"


@dataclass(frozen=True)
class AttentionItem:
    """Something a staff member should act on, with a link to where."""

    headline: str
    detail: str
    action_label: str
    action_url: str
    severity: str  # "warning" or "info"


@dataclass(frozen=True)
class DashboardSummary:
    """Every figure the dashboard shows."""

    available_animals: int
    total_animals: int
    pending_applications: int
    applications_under_review: int
    stale_applications: int
    open_invitations: int
    expired_invitations: int
    animals_without_applicants: int
    animals_without_suitable_applicants: int
    adoptions_in_progress: int
    analyses_completed: int
    attention_items: list[AttentionItem] = field(default_factory=list)
    recent_activity: list[ActivityEntry] = field(default_factory=list)

    @property
    def needs_attention(self) -> bool:
        """Whether anything requires staff action right now."""
        return bool(self.attention_items)


@dataclass(frozen=True)
class GetDashboardSummaryQuery(Query):
    """Fetch the operational dashboard."""


class GetDashboardSummaryHandler(QueryHandler[DashboardSummary]):
    """Answers GetDashboardSummaryQuery."""

    def handle(self, query: Query, session: Session) -> DashboardSummary:
        """Assemble the dashboard.

        Reads as a sequence of named steps rather than one long method, so a
        reader can see what the dashboard is made of without following the
        SQL.
        """
        assert isinstance(query, GetDashboardSummaryQuery)

        now = datetime.now(UTC).replace(tzinfo=None)

        animals_by_status = _count_animals_by_status(session)
        applications_by_status = _count_applications_by_status(session)
        invitations_by_status = _count_invitations_by_status(session)

        available = animals_by_status.get(AnimalStatus.AVAILABLE.value, 0)
        pending = applications_by_status.get(ApplicationStatus.SUBMITTED.value, 0)
        under_review = applications_by_status.get(ApplicationStatus.UNDER_REVIEW.value, 0)
        open_invitations = sum(
            invitations_by_status.get(status.value, 0)
            for status in InvitationStatus
            if status.is_awaiting_response
        )
        expired = invitations_by_status.get(InvitationStatus.EXPIRED.value, 0)

        without_applicants = _count_available_animals_without_applicants(session)
        without_suitable = _count_available_animals_without_suitable_applicants(session)
        stale = _count_stale_applications(session, now)

        summary_without_attention = DashboardSummary(
            available_animals=available,
            total_animals=sum(animals_by_status.values()),
            pending_applications=pending,
            applications_under_review=under_review,
            stale_applications=stale,
            open_invitations=open_invitations,
            expired_invitations=expired,
            animals_without_applicants=without_applicants,
            animals_without_suitable_applicants=without_suitable,
            adoptions_in_progress=animals_by_status.get(
                AnimalStatus.ADOPTION_IN_PROGRESS.value, 0
            ),
            analyses_completed=_count_analyses(session),
        )

        return DashboardSummary(
            **{
                **summary_without_attention.__dict__,
                "attention_items": _build_attention_items(summary_without_attention),
                "recent_activity": _recent_activity(session),
            }
        )


def _count_animals_by_status(session: Session) -> dict[str, int]:
    """Count animals grouped by status, in one query."""
    rows = session.execute(
        select(Animal.status, func.count()).group_by(Animal.status)
    ).all()
    return dict(rows)


def _count_applications_by_status(session: Session) -> dict[str, int]:
    """Count applications grouped by status, in one query."""
    rows = session.execute(
        select(AdoptionApplication.status, func.count()).group_by(
            AdoptionApplication.status
        )
    ).all()
    return dict(rows)


def _count_invitations_by_status(session: Session) -> dict[str, int]:
    """Count invitations grouped by status, in one query."""
    rows = session.execute(
        select(AdoptionInvitation.status, func.count()).group_by(
            AdoptionInvitation.status
        )
    ).all()
    return dict(rows)


def _count_analyses(session: Session) -> int:
    """How many match analyses the agent has produced."""
    return int(
        session.execute(select(func.count()).select_from(MatchAnalysis)).scalar_one()
    )


def _active_application_statuses() -> list[str]:
    """The statuses that count as an application still in play."""
    return [status.value for status in ApplicationStatus if status.is_active]


def _count_available_animals_without_applicants(session: Session) -> int:
    """Available animals nobody has applied for (spec section 22).

    These are the animals at risk of being overlooked, which is precisely
    what proactive discovery exists to solve - so the figure links straight
    to it.
    """
    animals_with_applicants = select(AdoptionApplication.animal_id).where(
        AdoptionApplication.status.in_(_active_application_statuses())
    )
    return int(
        session.execute(
            select(func.count())
            .select_from(Animal)
            .where(Animal.status == AnimalStatus.AVAILABLE.value)
            .where(Animal.animal_id.notin_(animals_with_applicants))
        ).scalar_one()
    )


def _count_available_animals_without_suitable_applicants(session: Session) -> int:
    """Available animals whose applicants all score below the threshold.

    Different from having no applicants at all, and spec section 22 asks for
    both: an animal with three unsuitable applicants needs the same action as
    one with none, but the staff member's reading of the situation differs.

    Counted from stored analyses, so an animal whose applicants have not been
    analysed yet is not reported as unsuitable - absence of evidence is not
    evidence of a poor match.
    """
    animals_with_applicants = set(
        session.execute(
            select(AdoptionApplication.animal_id)
            .where(AdoptionApplication.status.in_(_active_application_statuses()))
            .distinct()
        ).scalars()
    )
    if not animals_with_applicants:
        return 0

    available_ids = set(
        session.execute(
            select(Animal.animal_id).where(Animal.status == AnimalStatus.AVAILABLE.value)
        ).scalars()
    )

    best_score_by_animal = {
        animal_id: int(score)
        for animal_id, score in session.execute(
            select(MatchAnalysis.animal_id, func.max(MatchAnalysis.score)).group_by(
                MatchAnalysis.animal_id
            )
        ).all()
    }

    return sum(
        1
        for animal_id in animals_with_applicants & available_ids
        if animal_id in best_score_by_animal
        and best_score_by_animal[animal_id] < RECOMMENDATION_THRESHOLD
    )


def _count_stale_applications(session: Session, now: datetime) -> int:
    """Applications submitted a week ago and still untouched."""
    cutoff = now - timedelta(days=STALE_APPLICATION_DAYS)
    return int(
        session.execute(
            select(func.count())
            .select_from(AdoptionApplication)
            .where(AdoptionApplication.status == ApplicationStatus.SUBMITTED.value)
            .where(AdoptionApplication.submitted_at < cutoff)
        ).scalar_one()
    )


def _build_attention_items(summary: DashboardSummary) -> list[AttentionItem]:
    """Turn the figures into actions (spec section 22).

    This is what makes the page operational rather than decorative: a number
    with nowhere to click is a report, not a dashboard.
    """
    items: list[AttentionItem] = []

    if summary.stale_applications:
        items.append(
            AttentionItem(
                headline=f"{summary.stale_applications} application(s) waiting over a week",
                detail="These were submitted more than seven days ago and are still untouched.",
                action_label="Review applications",
                action_url="/animals/manage",
                severity="warning",
            )
        )

    if summary.expired_invitations:
        items.append(
            AttentionItem(
                headline=f"{summary.expired_invitations} invitation(s) expired unanswered",
                detail=(
                    "The 72-hour window closed without a response. These adopters "
                    "may still be worth contacting about another animal."
                ),
                action_label="See animals",
                action_url="/animals/manage",
                severity="info",
            )
        )

    if summary.animals_without_applicants:
        items.append(
            AttentionItem(
                headline=f"{summary.animals_without_applicants} animal(s) with no applicants",
                detail=(
                    "Nobody has applied for these. Proactive discovery can find "
                    "suitable adopters who have not seen them."
                ),
                action_label="Find adopters",
                action_url="/animals/manage?status=AVAILABLE",
                severity="warning",
            )
        )

    if summary.animals_without_suitable_applicants:
        items.append(
            AttentionItem(
                headline=(
                    f"{summary.animals_without_suitable_applicants} animal(s) with no "
                    f"suitable applicant"
                ),
                detail=(
                    "These have applicants, but none scores above the recommendation "
                    "threshold. Widening the search may help."
                ),
                action_label="Find more adopters",
                action_url="/animals/manage?status=AVAILABLE",
                severity="warning",
            )
        )

    if summary.pending_applications:
        items.append(
            AttentionItem(
                headline=f"{summary.pending_applications} application(s) awaiting a first look",
                detail="Newly submitted applications nobody has opened yet.",
                action_label="Open the roster",
                action_url="/animals/manage",
                severity="info",
            )
        )

    return items


def _recent_activity(session: Session) -> list[ActivityEntry]:
    """Build the activity feed from the event log (FR-6.4).

    The log is the source of truth for what happened, so the feed reads it
    directly rather than maintaining a parallel audit trail that could drift.
    """
    events = EventStore(session).read_recent(limit=RECENT_ACTIVITY_LIMIT)
    if not events:
        return []

    actor_names = _resolve_actor_names(session, events)
    animal_names = _resolve_animal_names(session, events)

    entries: list[ActivityEntry] = []
    for event in events:
        animal_id = event.payload.get("animal_id")
        entries.append(
            ActivityEntry(
                occurred_at=event.occurred_at,
                actor_name=actor_names.get(event.actor_user_id or "", "The system"),
                description=EVENT_DESCRIPTIONS.get(
                    event.event_type, event.event_type.value
                ),
                animal_name=animal_names.get(animal_id) if animal_id else None,
                animal_id=animal_id,
            )
        )
    return entries


def _resolve_actor_names(session: Session, events: list) -> dict[str, str]:
    """Look up every actor named in the feed, in one query."""
    actor_ids = {event.actor_user_id for event in events if event.actor_user_id}
    if not actor_ids:
        return {}

    rows = session.execute(
        select(User.user_id, User.full_name).where(User.user_id.in_(actor_ids))
    ).all()
    return dict(rows)


def _resolve_animal_names(session: Session, events: list) -> dict[str, str]:
    """Look up every animal named in the feed, in one query."""
    animal_ids = {
        event.payload.get("animal_id")
        for event in events
        if event.payload.get("animal_id")
    }
    if not animal_ids:
        return {}

    rows = session.execute(
        select(Animal.animal_id, Animal.name).where(Animal.animal_id.in_(animal_ids))
    ).all()
    return dict(rows)
