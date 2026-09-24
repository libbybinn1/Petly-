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
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.cqrs.base import Query, QueryHandler
from app.cqrs.queries.formatting import DisplayDate, to_display_moment
from app.domain.enums import (
    AnalysisJobStatus,
    AnimalStatus,
    ApplicationStatus,
    DomainEventType,
    InvitationStatus,
)
from app.domain.matching import RECOMMENDATION_THRESHOLD
from app.eventstore.store import EventStore, RecordedEvent
from app.infrastructure.clock import utc_now
from app.infrastructure.models import (
    AdoptionApplication,
    AdoptionInvitation,
    AnalysisJob,
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
    DomainEventType.ANIMAL_LISTED: "listed",
    DomainEventType.ANIMAL_UPDATED: "updated the record for",
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
    """One line of the recent-activity feed, derived from an event.

    `animal_id` and `animal_name` are set together or not at all. An event
    naming an animal that has since been deleted used to carry the
    identifier with no name, and the feed rendered "Staff Member approved an
    adoption of" followed by a link with no text - a truncated sentence and
    an invisible link. Either the animal can be named and linked, or the
    sentence ends where it ends.
    """

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

    @property
    def occurred(self) -> DisplayDate:
        """When this happened, in the three forms the feed needs."""
        return to_display_moment(self.occurred_at)


@dataclass(frozen=True)
class StatTile:
    """One headline figure, with somewhere to act on it (spec section 22).

    `action_url` is optional and honestly so: three of these figures have no
    screen of their own to open yet - there is no cross-animal application
    queue and no staff invitation list - and a tile that looks clickable and
    goes nowhere useful is worse than one that does not.
    """

    value: int
    label: str
    action_url: str | None = None
    action_label: str | None = None

    @property
    def is_actionable(self) -> bool:
        """Whether this tile should render as a link."""
        return bool(self.action_url and self.action_label)


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
    # The agent's queue. Staff cannot see the agent's terminal, so a stuck
    # or failing worker was invisible from the application: explanations
    # simply never appeared and nothing said why (CLAUDE.md R4).
    analyses_pending: int = 0
    analyses_failed: int = 0
    analyses_queued_oldest_at: datetime | None = None
    attention_items: list[AttentionItem] = field(default_factory=list)
    recent_activity: list[ActivityEntry] = field(default_factory=list)

    @property
    def needs_attention(self) -> bool:
        """Whether anything requires staff action right now."""
        return bool(self.attention_items)

    @property
    def agent_is_working(self) -> bool:
        """Whether the agent has anything outstanding."""
        return self.analyses_pending > 0

    @property
    def analyses_queued_oldest(self) -> DisplayDate | None:
        """When the longest-waiting job was queued, ready to display."""
        if self.analyses_queued_oldest_at is None:
            return None
        return to_display_moment(self.analyses_queued_oldest_at)

    @property
    def tiles(self) -> list[StatTile]:
        """The headline figures, in the order the dashboard shows them.

        A property rather than a stored field so a tile cannot disagree with
        the count it displays: there is one number, and the tile is a view of
        it.
        """
        roster = "/animals/manage"
        return [
            StatTile(
                self.available_animals,
                "Available for adoption",
                f"{roster}?status={AnimalStatus.AVAILABLE.value}",
                "Manage available animals",
            ),
            StatTile(
                self.pending_applications,
                "Applications awaiting review",
                roster,
                "Open the roster",
            ),
            StatTile(
                self.applications_under_review,
                "Under review",
                roster,
                "Open the roster",
            ),
            # No staff-side invitation list exists yet, so this one is a
            # figure rather than a link. See the report accompanying E-8.
            StatTile(self.open_invitations, "Invitations awaiting a reply"),
            StatTile(
                self.adoptions_in_progress,
                "Adoptions in progress",
                f"{roster}?status={AnimalStatus.ADOPTION_IN_PROGRESS.value}",
                "See adoptions in progress",
            ),
            # Likewise: analyses are reached through the animal they belong
            # to, not from an index of their own.
            StatTile(self.analyses_completed, "Match analyses completed"),
        ]


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

        now = utc_now()

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
        jobs_by_status = _count_analysis_jobs_by_status(session)

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
            analyses_pending=sum(
                jobs_by_status.get(status.value, 0) for status in _OUTSTANDING_JOB_STATUSES
            ),
            analyses_failed=jobs_by_status.get(AnalysisJobStatus.FAILED.value, 0),
            analyses_queued_oldest_at=_oldest_outstanding_job_at(session),
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
    ).tuples().all()
    return dict(rows)


def _count_applications_by_status(session: Session) -> dict[str, int]:
    """Count applications grouped by status, in one query."""
    rows = session.execute(
        select(AdoptionApplication.status, func.count()).group_by(
            AdoptionApplication.status
        )
    ).tuples().all()
    return dict(rows)


def _count_invitations_by_status(session: Session) -> dict[str, int]:
    """Count invitations grouped by status, in one query."""
    rows = session.execute(
        select(AdoptionInvitation.status, func.count()).group_by(
            AdoptionInvitation.status
        )
    ).tuples().all()
    return dict(rows)


def _count_analyses(session: Session) -> int:
    """How many match analyses the agent has produced."""
    return int(
        session.execute(select(func.count()).select_from(MatchAnalysis)).scalar_one()
    )


# A job the dashboard is still waiting on. IN_PROGRESS counts as pending
# because from the outside there is no difference: no explanation yet.
_OUTSTANDING_JOB_STATUSES = (AnalysisJobStatus.PENDING, AnalysisJobStatus.IN_PROGRESS)


def _count_analysis_jobs_by_status(session: Session) -> dict[str, int]:
    """Count the agent's queue grouped by status, in one query."""
    rows = session.execute(
        select(AnalysisJob.status, func.count()).group_by(AnalysisJob.status)
    ).tuples().all()
    return dict(rows)


def _oldest_outstanding_job_at(session: Session) -> datetime | None:
    """When the longest-waiting agent job was queued, or None if none is.

    The figure that distinguishes a busy queue from a stopped one: a pending
    count alone looks the same whether the worker is thirty seconds behind
    or was killed yesterday.
    """
    oldest: datetime | None = session.execute(
        select(func.min(AnalysisJob.created_at)).where(
            AnalysisJob.status.in_(
                [status.value for status in _OUTSTANDING_JOB_STATUSES]
            )
        )
    ).scalar_one_or_none()
    return oldest


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

    # Restricted to the adopters who actually applied. Taking MAX over every
    # analysis for the animal counted discovery scores for adopters who never
    # applied, so one enthusiastic non-applicant hid an animal whose real
    # applicants all scored badly - precisely the animal this tile exists to
    # surface.
    best_score_by_animal = {
        animal_id: int(score)
        for animal_id, score in session.execute(
            select(MatchAnalysis.animal_id, func.max(MatchAnalysis.score))
            .join(
                AdoptionApplication,
                (AdoptionApplication.animal_id == MatchAnalysis.animal_id)
                & (
                    AdoptionApplication.adopter_profile_id
                    == MatchAnalysis.adopter_profile_id
                ),
            )
            .where(AdoptionApplication.status.in_(_active_application_statuses()))
            .group_by(MatchAnalysis.animal_id)
        )
        .tuples()
        .all()
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


def _count_phrase(quantity: int, singular: str, plural: str) -> str:
    """Phrase a count with the right noun, and never with "(s)".

    "1 application(s) waiting over a week" is the kind of line that tells a
    reader the software was written in a hurry, and it appeared on the most
    senior screen in the product. Both forms are written out because English
    plurals are not a suffix rule ("animal with no suitable applicant"
    becomes "animals with no suitable applicant", not "applicants").

    Args:
        quantity: How many.
        singular: The phrase following "1".
        plural: The phrase following any other number.

    Returns:
        The count and its noun phrase.
    """
    return f"{quantity} {singular if quantity == 1 else plural}"


def _build_attention_items(summary: DashboardSummary) -> list[AttentionItem]:
    """Turn the figures into actions (spec section 22).

    This is what makes the page operational rather than decorative: a number
    with nowhere to click is a report, not a dashboard.
    """
    items: list[AttentionItem] = []

    if summary.stale_applications:
        items.append(
            AttentionItem(
                headline=_count_phrase(
                    summary.stale_applications,
                    "application waiting over a week",
                    "applications waiting over a week",
                ),
                detail="These were submitted more than seven days ago and are still untouched.",
                action_label="Review applications",
                action_url="/animals/manage",
                severity="warning",
            )
        )

    if summary.expired_invitations:
        items.append(
            AttentionItem(
                headline=_count_phrase(
                    summary.expired_invitations,
                    "invitation expired unanswered",
                    "invitations expired unanswered",
                ),
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
                headline=_count_phrase(
                    summary.animals_without_applicants,
                    "animal with no applicants",
                    "animals with no applicants",
                ),
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
                headline=_count_phrase(
                    summary.animals_without_suitable_applicants,
                    "animal with no suitable applicant",
                    "animals with no suitable applicant",
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
                headline=_count_phrase(
                    summary.pending_applications,
                    "application awaiting a first look",
                    "applications awaiting a first look",
                ),
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
        animal_name = animal_names.get(animal_id) if isinstance(animal_id, str) else None
        entries.append(
            ActivityEntry(
                occurred_at=event.occurred_at,
                actor_name=actor_names.get(event.actor_user_id or "", "The system"),
                description=EVENT_DESCRIPTIONS.get(
                    event.event_type, event.event_type.value
                ),
                animal_name=animal_name,
                # Dropped when the animal cannot be named: the two travel
                # together or the feed renders a link with no text after a
                # sentence with no object. See ActivityEntry.
                animal_id=animal_id if animal_name else None,
            )
        )
    return entries


def _resolve_actor_names(
    session: Session, events: list[RecordedEvent]
) -> dict[str, str]:
    """Look up every actor named in the feed, in one query."""
    actor_ids = {event.actor_user_id for event in events if event.actor_user_id}
    if not actor_ids:
        return {}

    rows = session.execute(
        select(User.user_id, User.full_name).where(User.user_id.in_(actor_ids))
    ).tuples().all()
    return dict(rows)


def _resolve_animal_names(
    session: Session, events: list[RecordedEvent]
) -> dict[str, str]:
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
    ).tuples().all()
    return dict(rows)
