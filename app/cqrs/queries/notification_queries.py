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
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cqrs.base import Query, QueryHandler
from app.domain.enums import NotificationType
from app.infrastructure.models import Notification

# How many messages the inbox shows at once. Old notifications are history
# rather than a to-do list, so there is no paginator: the recent ones are
# the ones anybody acts on.
INBOX_LIMIT = 50

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
        nothing and the phrasing is testable.
        """
        return _relative_time(self.created_at)


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


def _relative_time(moment: datetime) -> str:
    """Describe how long ago something happened.

    Args:
        moment: A naive UTC timestamp, as the DATETIME2 columns store.

    Returns:
        A short phrase such as "2 hours ago".
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    aware_moment = moment.replace(tzinfo=None) if moment.tzinfo else moment
    seconds = (now - aware_moment).total_seconds()

    if seconds < 60:
        return "just now"
    if seconds < 3600:
        minutes = int(seconds // 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"

    days = int(seconds // 86400)
    if days < 30:
        return f"{days} day{'s' if days != 1 else ''} ago"
    months = days // 30
    return f"{months} month{'s' if months != 1 else ''} ago"
