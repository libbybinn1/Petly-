"""Read-side query for the account behind a sign-in attempt.

The sign-in controller used to open a session and query the tables
directly. CLAUDE.md R2 says in as many words that a controller must never
touch a DB session, and the docstring defending the exception argued that
authentication is not a business read.

That reasoning does not hold. Verifying a password is a query with an
unusual result, not a different kind of thing, and "this one is special"
is how a layer boundary erodes. The lookup is a query like any other; the
controller keeps only `check_password_hash`, which compares two strings
and touches no storage.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cqrs.base import Query, QueryHandler
from app.infrastructure.models import AdopterProfile, User


@dataclass(frozen=True)
class AccountForSignIn:
    """Everything sign-in needs about one account.

    Carries the password hash, which is the one piece of stored data the
    controller legitimately handles: comparing it is authentication, and
    it never leaves this request. It is deliberately not on any other
    read model, so no screen can render it by accident.
    """

    user_id: str
    email: str
    full_name: str
    role: str
    is_active: bool
    password_hash: str
    adopter_profile_id: str | None
    has_complete_profile: bool


@dataclass(frozen=True)
class GetAccountForSignInQuery(Query):
    """Look up the account matching a submitted email address."""

    email: str


class GetAccountForSignInHandler(QueryHandler[AccountForSignIn | None]):
    """Answers GetAccountForSignInQuery."""

    def handle(self, query: Query, session: Session) -> AccountForSignIn | None:
        """Fetch the account and its profile in one pass.

        Returns None for an unknown address rather than raising. The
        controller answers the same way for an unknown address and a wrong
        password, so that the form cannot be used to discover which
        addresses are registered - and that only works if this query
        treats "no such account" as an ordinary answer.

        Returns:
            The account, or None when no account has that address.
        """
        assert isinstance(query, GetAccountForSignInQuery)

        user = session.execute(
            select(User).where(User.email == query.email)
        ).scalar_one_or_none()
        if user is None:
            return None

        profile = session.execute(
            select(AdopterProfile).where(AdopterProfile.user_id == user.user_id)
        ).scalar_one_or_none()

        return AccountForSignIn(
            user_id=user.user_id,
            email=user.email,
            full_name=user.full_name,
            role=user.role,
            is_active=bool(user.is_active),
            password_hash=user.password_hash,
            adopter_profile_id=profile.adopter_profile_id if profile else None,
            has_complete_profile=bool(profile and profile.is_complete),
        )


@dataclass(frozen=True)
class EmailIsRegisteredQuery(Query):
    """Whether an address already has an account."""

    email: str


class EmailIsRegisteredHandler(QueryHandler[bool]):
    """Answers EmailIsRegisteredQuery."""

    def handle(self, query: Query, session: Session) -> bool:
        """Report whether the address is taken.

        Used to answer a registration attempt politely. It is not what
        prevents a duplicate: two overlapping registrations would both
        pass this check, and the unique index on `users.email` is what
        actually settles that race.
        """
        assert isinstance(query, EmailIsRegisteredQuery)

        existing = session.execute(
            select(User.user_id).where(User.email == query.email)
        ).scalar_one_or_none()
        return existing is not None
