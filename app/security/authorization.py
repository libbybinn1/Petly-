"""Authentication helpers and server-side role enforcement.

Course blueprint section 12 is explicit: the UI, API and server must all
enforce authorization, and hiding buttons in the interface is not sufficient.
The decorators here run on every protected request, so a forged form post
from an adopter to a staff endpoint is rejected with 403 regardless of what
the rendered page offered.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from flask import abort
from flask_login import UserMixin, current_user

from app.domain.enums import UserRole
from app.infrastructure.models import User

P = ParamSpec("P")
R = TypeVar("R")


class AuthenticatedUser(UserMixin):
    """Flask-Login view of a signed-in account.

    Deliberately a small copy rather than the ORM model: the session outlives
    any database session, and detached ORM instances raise when their
    attributes are touched later in the request.
    """

    def __init__(
        self,
        user_id: str,
        email: str,
        full_name: str,
        role: UserRole,
        adopter_profile_id: str | None,
        has_complete_profile: bool,
    ) -> None:
        """Capture the identity fields needed across a request."""
        self.id = user_id
        self.user_id = user_id
        self.email = email
        self.full_name = full_name
        self.role = role
        self.adopter_profile_id = adopter_profile_id
        self.has_complete_profile = has_complete_profile

    @property
    def is_staff(self) -> bool:
        """Whether this account may perform staff operations."""
        return self.role is UserRole.STAFF

    @property
    def is_adopter(self) -> bool:
        """Whether this account is an adopter."""
        return self.role is UserRole.ADOPTER

    @property
    def initials(self) -> str:
        """Two-letter initials for the avatar badge."""
        parts = [part for part in self.full_name.split() if part]
        if not parts:
            return "?"
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()

    @classmethod
    def from_model(
        cls, user: User, adopter_profile_id: str | None, is_complete: bool
    ) -> AuthenticatedUser:
        """Build an authenticated user from an ORM row."""
        return cls(
            user_id=user.user_id,
            email=user.email,
            full_name=user.full_name,
            role=UserRole(user.role),
            adopter_profile_id=adopter_profile_id,
            has_complete_profile=is_complete,
        )


def require_role(*allowed_roles: UserRole) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Reject requests from accounts outside the allowed roles.

    Applied at the controller boundary. Returns 401 when nobody is signed in
    and 403 when the signed-in account holds the wrong role, so an adopter
    cannot reach a staff endpoint by posting to it directly.

    Args:
        allowed_roles: Roles permitted to invoke the view.

    Returns:
        A decorator enforcing the restriction.
    """

    def decorator(view_function: Callable[P, R]) -> Callable[P, R]:
        @wraps(view_function)
        def wrapper(*args: Any, **kwargs: Any) -> R:  # noqa: ANN401 - Flask view args
            if not current_user.is_authenticated:
                abort(401)
            if current_user.role not in allowed_roles:
                abort(403)
            return view_function(*args, **kwargs)

        return wrapper

    return decorator


def require_staff() -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Restrict a view to organization staff."""
    return require_role(UserRole.STAFF)


def require_adopter() -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Restrict a view to adopters."""
    return require_role(UserRole.ADOPTER)
