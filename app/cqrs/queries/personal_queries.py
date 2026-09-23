"""Read-side queries for an adopter's own records.

Every query here is scoped to one adopter and takes their profile identifier
as a parameter rather than a record identifier. That is deliberate: it makes
it impossible to express "fetch somebody else's invitation", so ownership is
enforced by the shape of the query rather than by a check somebody might
forget (FR-2.4).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cqrs.base import Query, QueryHandler
from app.domain.enums import ApplicationStatus, InvitationStatus
from app.domain.invitation_rules import has_expired
from app.infrastructure.models import (
    AdoptionApplication,
    AdoptionInvitation,
    Animal,
    Notification,
    User,
)
from app.infrastructure.sql_helpers import is_false

HOURS_PER_DAY = 24


@dataclass(frozen=True)
class InvitationSummary:
    """One invitation as the adopter sees it."""

    invitation_id: str
    animal_id: str
    animal_name: str
    animal_species: str
    animal_image_url: str | None
    sent_by_name: str
    staff_message: str | None
    status: str
    sent_at: datetime
    expires_at: datetime
    hours_remaining: float

    @property
    def is_awaiting_response(self) -> bool:
        """Whether the adopter can still act on this invitation."""
        return InvitationStatus(self.status).is_awaiting_response

    @property
    def is_expired(self) -> bool:
        """Whether the window has closed."""
        return InvitationStatus(self.status) is InvitationStatus.EXPIRED

    @property
    def time_remaining_label(self) -> str:
        """Human phrasing of the time left, for the countdown badge."""
        if not self.is_awaiting_response:
            return ""
        if self.hours_remaining <= 0:
            return "Expired"
        if self.hours_remaining < 1:
            return f"{int(self.hours_remaining * 60)} minutes left"
        if self.hours_remaining < HOURS_PER_DAY:
            return f"{int(self.hours_remaining)} hours left"
        return f"{int(self.hours_remaining // HOURS_PER_DAY)} days left"

    @property
    def is_urgent(self) -> bool:
        """Whether the deadline is close enough to highlight."""
        return self.is_awaiting_response and 0 < self.hours_remaining <= HOURS_PER_DAY


@dataclass(frozen=True)
class ApplicationSummary:
    """One application as the adopter sees it."""

    application_id: str
    animal_id: str
    animal_name: str
    animal_species: str
    animal_image_url: str | None
    status: str
    applicant_message: str | None
    submitted_at: datetime
    came_from_invitation: bool

    @property
    def is_active(self) -> bool:
        """Whether this application is still in play."""
        return ApplicationStatus(self.status).is_active


@dataclass(frozen=True)
class ListMyInvitationsQuery(Query):
    """Every invitation belonging to one adopter."""

    adopter_profile_id: str


@dataclass(frozen=True)
class ListMyApplicationsQuery(Query):
    """Every application belonging to one adopter."""

    adopter_profile_id: str


@dataclass(frozen=True)
class CountUnreadNotificationsQuery(Query):
    """How many unread inbox messages one user has."""

    user_id: str


class ListMyInvitationsHandler(QueryHandler[list[InvitationSummary]]):
    """Answers ListMyInvitationsQuery."""

    def handle(self, query: Query, session: Session) -> list[InvitationSummary]:
        """Return the adopter's invitations, newest first."""
        assert isinstance(query, ListMyInvitationsQuery)

        rows = (
            session.execute(
                select(AdoptionInvitation)
                .where(AdoptionInvitation.adopter_profile_id == query.adopter_profile_id)
                .order_by(AdoptionInvitation.sent_at.desc())
            )
            .scalars()
            .all()
        )

        now = datetime.now(UTC)
        summaries: list[InvitationSummary] = []

        for row in rows:
            animal = session.get(Animal, row.animal_id)
            sender = session.get(User, row.sent_by_user_id)
            expires_at = row.expires_at.replace(tzinfo=UTC)

            summaries.append(
                InvitationSummary(
                    invitation_id=row.invitation_id,
                    animal_id=row.animal_id,
                    animal_name=animal.name if animal else "Unknown animal",
                    animal_species=animal.species if animal else "",
                    animal_image_url=animal.primary_image_url if animal else None,
                    sent_by_name=sender.full_name if sender else "The organization",
                    staff_message=row.staff_message,
                    status=row.status,
                    sent_at=row.sent_at.replace(tzinfo=UTC),
                    expires_at=expires_at,
                    hours_remaining=_hours_until(expires_at, now),
                )
            )

        return summaries


class ListMyApplicationsHandler(QueryHandler[list[ApplicationSummary]]):
    """Answers ListMyApplicationsQuery."""

    def handle(self, query: Query, session: Session) -> list[ApplicationSummary]:
        """Return the adopter's applications, newest first."""
        assert isinstance(query, ListMyApplicationsQuery)

        rows = (
            session.execute(
                select(AdoptionApplication)
                .where(
                    AdoptionApplication.adopter_profile_id == query.adopter_profile_id
                )
                .order_by(AdoptionApplication.submitted_at.desc())
            )
            .scalars()
            .all()
        )

        summaries: list[ApplicationSummary] = []
        for row in rows:
            animal = session.get(Animal, row.animal_id)
            summaries.append(
                ApplicationSummary(
                    application_id=row.application_id,
                    animal_id=row.animal_id,
                    animal_name=animal.name if animal else "Unknown animal",
                    animal_species=animal.species if animal else "",
                    animal_image_url=animal.primary_image_url if animal else None,
                    status=row.status,
                    applicant_message=row.applicant_message,
                    submitted_at=row.submitted_at.replace(tzinfo=UTC),
                    came_from_invitation=row.originating_invitation_id is not None,
                )
            )

        return summaries


class CountUnreadNotificationsHandler(QueryHandler[int]):
    """Answers CountUnreadNotificationsQuery."""

    def handle(self, query: Query, session: Session) -> int:
        """Count one user's unread inbox messages."""
        assert isinstance(query, CountUnreadNotificationsQuery)

        rows = (
            session.execute(
                select(Notification.notification_id)
                .where(Notification.user_id == query.user_id)
                .where(is_false(Notification.is_read))
            )
            .scalars()
            .all()
        )
        return len(rows)


def _hours_until(moment: datetime, now: datetime) -> float:
    """Hours remaining until a moment, or 0 once it has passed."""
    if has_expired(moment, now):
        return 0.0
    return (moment - now).total_seconds() / 3600
