"""Read-side queries for the internal notification inbox (spec section 23).

Notifications were being written from the moment applications and
invitations existed, and there was nowhere to read them: an adopter whose
application was approved was told so by a row nobody could see.

No email integration, by design - spec section 23 asks for an inbox inside
the application, which is also the only thing testable without a mail
server.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cqrs.base import Query, QueryHandler
from app.cqrs.queries.formatting import describe_relative_time
from app.domain.enums import NotificationType
from app.infrastructure.clock import as_aware_utc, aware_utc_now
from app.infrastructure.models import Notification

# How many messages the inbox shows at once. Old notifications are history
# rather than a to-do list, so there is no paginator: the recent ones are
# the ones anybody acts on.
INBOX_LIMIT = 50

# The window the inbox calls "last 24 hours". A fixed duration rather than a
# calendar day: the column is UTC and the reader's day is not, so "today"
# would mean different things in different timezones.
RECENT_NOTIFICATION_WINDOW = timedelta(hours=24)

# What each kind of message is called on screen. The enum values are
# shouted constants; these are what a person reads.
NOTIFICATION_LABELS: dict[NotificationType, str] = {
    NotificationType.INVITATION_RECEIVED: "Invitation",
    NotificationType.INVITATION_RESPONSE: "Invitation answered",
    NotificationType.APPLICATION_STATUS_CHANGED: "Application update",
    NotificationType.ANALYSIS_READY: "Match explanation ready",
}


@dataclass(frozen=True)
class NotificationItem:
    """One inbox message, shaped for the template."""

    notification_id: str
    notification_type: str
    label: str
    title: str
    body: str
    target_url: str | None
    is_read: bool
    created_at: datetime

    @property
    def relative_label(self) -> str:
        """How long ago this arrived, in words.

        Computed here rather than in the template, so the view decides
        nothing and the phrasing is testable. Shared with every other
        timestamp on the site (`app.cqrs.queries.formatting`): this module
        used to carry its own copy, with its own vocabulary, and the inbox
        was the one screen that said "1 month ago" where the rest said
        "on 23 Sep 2026".
        """
        return describe_relative_time(self.created_at)

    @property
    def is_recent(self) -> bool:
        """Whether this arrived inside the last day (docs/UX.md section 3).

        The inbox groups messages into "last 24 hours" and "earlier". That
        grouping was being derived in the template by looking for the words
        "day", "month" and "year" inside `relative_label` - which decided
        something in a view (rule R2), and quietly depended on the phrasing
        of a label nobody thought of as an interface. It was correct only by
        accident, and changing the label to the shared formatter would have
        broken it: past a week that says "on 23 Sep 2026", which contains
        none of the three words.
        """
        return aware_utc_now() - as_aware_utc(self.created_at) < RECENT_NOTIFICATION_WINDOW


@dataclass(frozen=True)
class ListMyNotificationsQuery(Query):
    """Fetch the signed-in user's inbox."""

    user_id: str
    unread_only: bool = False


class ListMyNotificationsHandler(QueryHandler[list[NotificationItem]]):
    """Answers ListMyNotificationsQuery."""

    def handle(self, query: Query, session: Session) -> list[NotificationItem]:
        """Return the user's messages, newest first.

        Scoped to `user_id` with no identifier taken from the URL, so
        reading somebody else's inbox is not expressible rather than
        merely forbidden (FR-2.4).
        """
        assert isinstance(query, ListMyNotificationsQuery)

        statement = (
            select(Notification)
            .where(Notification.user_id == query.user_id)
            .order_by(Notification.created_at.desc())
            .limit(INBOX_LIMIT)
        )
        if query.unread_only:
            statement = statement.where(Notification.is_read.is_(False))

        rows = session.execute(statement).scalars().all()
        return [_to_item(row) for row in rows]


def _to_item(row: Notification) -> NotificationItem:
    """Convert a stored notification into its view model."""
    try:
        kind = NotificationType(row.notification_type)
        label = NOTIFICATION_LABELS[kind]
    except (ValueError, KeyError):
        # A type this version does not know is still worth showing; the
        # title and body carry the message either way.
        label = "Update"

    return NotificationItem(
        notification_id=row.notification_id,
        notification_type=row.notification_type,
        label=label,
        title=row.title,
        body=row.body,
        target_url=row.link_url,
        is_read=bool(row.is_read),
        created_at=row.created_at,
    )
