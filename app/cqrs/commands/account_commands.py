"""Write-side commands for accounts (spec section 4).

Registration used to insert a `User` row from inside the controller, which
CLAUDE.md R2 forbids: a controller dispatches and never holds a session.

No domain event is appended here, deliberately. The event log covers the
processes the specification names - applications, invitations and the
approval cascade - and creating an account is not one of them. Adding a
`UserRegistered` event would mean adding a `User` aggregate type that only
one writer produces and no reader understands, which is a worse outcome
than leaving it out: the history view resolves an aggregate to a record,
and a type it cannot resolve would answer 404 for every account.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.cqrs.base import Command, CommandHandler
from app.domain.enums import UserRole
from app.infrastructure.models import User, new_identifier


class EmailAlreadyRegisteredError(ValueError):
    """Raised when an address already has an account."""


@dataclass(frozen=True)
class RegisterAdopterCommand(Command):
    """Create a new adopter account.

    Carries the already-hashed password. Hashing is an authentication
    concern the controller owns, and a plaintext password should travel
    no further into the application than it must.
    """

    email: str
    full_name: str
    password_hash: str


class RegisterAdopterHandler(CommandHandler[str]):
    """Creates one adopter account."""

    def handle(self, command: Command, session: Session) -> str:
        """Insert the account.

        Returns:
            The new user's identifier - an identifier, not a read model.
            The controller dispatches a query next if it needs more
            (rule R2).

        Raises:
            EmailAlreadyRegisteredError: The address is already taken.
        """
        assert isinstance(command, RegisterAdopterCommand)

        user_id = new_identifier()

        session.add(
            User(
                user_id=user_id,
                email=command.email,
                password_hash=command.password_hash,
                full_name=command.full_name,
                role=UserRole.ADOPTER.value,
                is_active=True,
                created_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )

        # Flushed here rather than left to the bus's commit, so a
        # duplicate address becomes a message this handler can phrase -
        # not a 500 raised after the controller already decided the
        # registration had worked.
        #
        # This is also what actually prevents a duplicate. Checking first
        # and inserting second is not atomic, and two overlapping
        # registrations both pass the check; the unique index on
        # users.email is what settles it.
        try:
            session.flush()
        except IntegrityError as error:
            raise EmailAlreadyRegisteredError(
                "An account with that email already exists."
            ) from error

        return user_id
