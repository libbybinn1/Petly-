"""Business rules for adoption invitations (spec sections 7.4 and 25).

An invitation is a staff-initiated suggestion with a 72-hour response window.
It is deliberately a separate process from an application: accepting an
invitation *starts* an application, it does not approve an adoption.

Pure domain logic, no framework and no session, so every rule here is
unit-testable in isolation (rule R2).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.domain.enums import AnimalStatus, InvitationStatus

# Spec section 7.4 fixes the response window at 72 hours. It is the default
# rather than a constant so a deployment can shorten it without editing the
# domain - `calculate_expiry` takes the window as an argument, which keeps
# this layer free of any configuration machinery (rule R2).
DEFAULT_RESPONSE_WINDOW_HOURS = 72
INVITATION_RESPONSE_WINDOW = timedelta(hours=DEFAULT_RESPONSE_WINDOW_HOURS)


class InvitationNotAllowedError(ValueError):
    """Raised when an invitation may not be sent."""


class InvitationExpiredError(ValueError):
    """Raised when someone responds after the window closed."""


@dataclass(frozen=True)
class InvitationEligibility:
    """The facts that decide whether an adopter may be invited."""

    adopter_profile_is_complete: bool
    adopter_opted_in: bool
    adopter_account_is_active: bool
    animal_status: AnimalStatus
    has_open_invitation_for_this_animal: bool
    has_active_application_for_this_animal: bool


def calculate_expiry(
    sent_at: datetime, window_hours: int = DEFAULT_RESPONSE_WINDOW_HOURS
) -> datetime:
    """Return the moment an invitation sent at `sent_at` stops accepting a reply.

    Args:
        sent_at: When the invitation was sent. Must be timezone-aware; the
            window is wrong if a naive value slips in, which is why ruff's
            DTZ rules are enabled on this project.
        window_hours: How long the adopter has. Defaults to the 72 hours
            spec section 7.4 fixes; the caller passes the configured value
            so INVITATION_EXPIRY_HOURS actually governs behaviour.

    Returns:
        The expiry instant.

    Raises:
        ValueError: If `sent_at` is naive, or the window is not positive.
    """
    if sent_at.tzinfo is None:
        raise ValueError("sent_at must be timezone-aware to compute a correct expiry.")
    if window_hours <= 0:
        raise ValueError("The response window must be a positive number of hours.")
    return sent_at + timedelta(hours=window_hours)


def has_expired(expires_at: datetime, now: datetime) -> bool:
    """Whether an invitation's window has closed.

    The boundary is inclusive of the final instant: an invitation is still
    answerable exactly at `expires_at` and expired one moment later.

    Both arguments must be timezone-aware, and this checks rather than
    assumes. The column behind `expires_at` is a SQL Server DATETIME2, which
    carries no offset, so every caller converts on the way out of the
    database. A caller that forgets would otherwise get a TypeError raised
    from inside the comparison - the same mistake `calculate_expiry` reports
    clearly, so it is reported clearly here too.

    Args:
        expires_at: When the invitation's window closes.
        now: The instant to judge it against.

    Returns:
        True once the window has passed.

    Raises:
        ValueError: If either argument is naive.
    """
    if expires_at.tzinfo is None:
        raise ValueError("expires_at must be timezone-aware to judge expiry.")
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware to judge expiry.")
    return now > expires_at


def ensure_invitation_may_be_sent(eligibility: InvitationEligibility) -> None:
    """Raise unless this adopter may be invited for this animal.

    Applies the eligibility rules of spec section 10, plus two practical ones:
    not inviting somebody twice over, and not inviting somebody who already
    applied - they are already in the workflow the invitation would start.

    Raises:
        InvitationNotAllowedError: If any rule fails. The message is written
            for a staff member to read, not a developer.
    """
    if not eligibility.adopter_profile_is_complete:
        raise InvitationNotAllowedError(
            "This adopter has not completed their adoption profile."
        )

    if not eligibility.adopter_opted_in:
        raise InvitationNotAllowedError(
            "This adopter has not opted in to receiving proactive suggestions."
        )

    if not eligibility.adopter_account_is_active:
        raise InvitationNotAllowedError("This adopter's account is not active.")

    if not eligibility.animal_status.is_open_for_applications:
        raise InvitationNotAllowedError(
            f"This animal is "
            f"{eligibility.animal_status.value.replace('_', ' ').lower()} "
            f"and cannot be offered."
        )

    if eligibility.has_open_invitation_for_this_animal:
        raise InvitationNotAllowedError(
            "This adopter already has an open invitation for this animal."
        )

    if eligibility.has_active_application_for_this_animal:
        raise InvitationNotAllowedError(
            "This adopter has already applied for this animal."
        )


def ensure_response_is_allowed(
    current_status: InvitationStatus, expires_at: datetime, now: datetime
) -> None:
    """Raise unless an adopter may still accept or decline.

    Raises:
        InvitationExpiredError: The window has closed.
        InvitationNotAllowedError: It was already answered.
    """
    if not current_status.is_awaiting_response:
        raise InvitationNotAllowedError(
            f"This invitation was already "
            f"{current_status.value.lower()} and cannot be answered again."
        )

    if has_expired(expires_at, now):
        raise InvitationExpiredError(
            "This invitation expired. Invitations are open for 72 hours."
        )


def status_after_expiry_sweep(
    current_status: InvitationStatus, expires_at: datetime, now: datetime
) -> InvitationStatus | None:
    """Return the new status for an invitation the sweep should expire.

    Args:
        current_status: The invitation's present status.
        expires_at: When its window closes.
        now: The current instant.

    Returns:
        `EXPIRED` when the invitation is unanswered and past its window,
        otherwise None, meaning leave it alone. Already-answered invitations
        are never touched - an accepted invitation does not expire.
    """
    if not current_status.is_awaiting_response:
        return None
    if not has_expired(expires_at, now):
        return None
    return InvitationStatus.EXPIRED
