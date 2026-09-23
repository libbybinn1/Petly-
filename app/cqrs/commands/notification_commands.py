"""Write-side commands for the notification inbox (spec section 23)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cqrs.base import Command, CommandHandler
from app.cqrs.commands.application_commands import (
    NotYourRecordError,
    RecordNotFoundError,
)
from app.infrastructure.models import Notification
from app.infrastructure.sql_helpers import is_false


@dataclass(frozen=True)
class MarkNotificationReadCommand(Command):
    """A user marks one of their own messages as read.

    Carries the reader's own identifier so the handler can prove the
    message is theirs. The identifier in the URL is attacker-controlled;
    this one comes from the session (FR-2.4).
    """

    notification_id: str
    user_id: str


class MarkNotificationReadHandler(CommandHandler[None]):
    """Marks one notification read."""

    def handle(self, command: Command, session: Session) -> None:
        """Mark the message read, if it belongs to this reader.

        The ownership check lives here rather than in the controller, so a
        second caller cannot bypass it by forgetting to repeat it.

        Raises:
            RecordNotFoundError: No such notification.
            NotYourRecordError: It belongs to somebody else.
        """
        assert isinstance(command, MarkNotificationReadCommand)

        notification = session.get(Notification, command.notification_id)
        if notification is None:
            raise RecordNotFoundError("Notification does not exist.")

        if notification.user_id != command.user_id:
            raise NotYourRecordError("This message belongs to another account.")

        notification.is_read = True


@dataclass(frozen=True)
class MarkAllNotificationsReadCommand(Command):
    """A user clears their whole inbox."""

    user_id: str


class MarkAllNotificationsReadHandler(CommandHandler[int]):
    """Marks every unread message for one user as read."""

    def handle(self, command: Command, session: Session) -> int:
        """Mark all of this user's unread messages read.

        Scoped to the signed-in user with no identifier from the URL, so
        clearing somebody else's inbox is not expressible.

        Returns:
            How many messages were marked.
        """
        assert isinstance(command, MarkAllNotificationsReadCommand)

        unread = (
            session.execute(
                select(Notification)
                .where(Notification.user_id == command.user_id)
                .where(is_false(Notification.is_read))
            )
            .scalars()
            .all()
        )

        for notification in unread:
            notification.is_read = True

        return len(unread)
