"""Business rules for the adoption application lifecycle.

Pure domain logic: no Flask, no SQLAlchemy, no database session. Every rule
here is decidable from values alone, which is what lets the state machine be
exhaustively unit-tested with no infrastructure (rule R2).

The lifecycle is specified in spec section 25 and docs/MODEL_DATA.md section 3.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.enums import AnimalStatus, ApplicationStatus, InvitationStatus

# Which statuses an application may move to from each current status.
# Anything absent from this table is illegal by construction, so adding a
# transition means adding it here first.
ALLOWED_APPLICATION_TRANSITIONS: dict[ApplicationStatus, frozenset[ApplicationStatus]] = {
    ApplicationStatus.SUBMITTED: frozenset(
        {
            ApplicationStatus.UNDER_REVIEW,
            ApplicationStatus.APPROVED,
            ApplicationStatus.REJECTED,
            ApplicationStatus.WITHDRAWN,
            ApplicationStatus.CLOSED,
        }
    ),
    ApplicationStatus.UNDER_REVIEW: frozenset(
        {
            ApplicationStatus.APPROVED,
            ApplicationStatus.REJECTED,
            ApplicationStatus.WITHDRAWN,
            ApplicationStatus.CLOSED,
        }
    ),
    # An approval can be reversed, which is what makes the spec 7.5 reopen
    # rule reachable at all.
    ApplicationStatus.APPROVED: frozenset(
        {ApplicationStatus.WITHDRAWN, ApplicationStatus.CLOSED}
    ),
    ApplicationStatus.REJECTED: frozenset(),
    ApplicationStatus.WITHDRAWN: frozenset(),
    # Structurally legal, but not sufficient on its own: only an
    # application closed *by* a specific approval may reopen when that
    # approval is reversed. `may_reopen` below decides which.
    ApplicationStatus.CLOSED: frozenset({ApplicationStatus.SUBMITTED}),
}

ALLOWED_INVITATION_TRANSITIONS: dict[InvitationStatus, frozenset[InvitationStatus]] = {
    InvitationStatus.SENT: frozenset(
        {
            InvitationStatus.VIEWED,
            InvitationStatus.ACCEPTED,
            InvitationStatus.DECLINED,
            InvitationStatus.EXPIRED,
        }
    ),
    InvitationStatus.VIEWED: frozenset(
        {InvitationStatus.ACCEPTED, InvitationStatus.DECLINED, InvitationStatus.EXPIRED}
    ),
    InvitationStatus.ACCEPTED: frozenset(),
    InvitationStatus.DECLINED: frozenset(),
    InvitationStatus.EXPIRED: frozenset(),
}


class IllegalTransitionError(ValueError):
    """Raised when a state change is not permitted from the current state."""


class ApplicationNotAllowedError(ValueError):
    """Raised when an application may not be submitted."""


@dataclass(frozen=True)
class ApplicationSnapshot:
    """The facts about one application that the rules depend on."""

    application_id: str
    adopter_profile_id: str
    animal_id: str
    status: ApplicationStatus
    closed_because_application_id: str | None = None


def ensure_application_transition_allowed(
    current: ApplicationStatus, target: ApplicationStatus
) -> None:
    """Raise unless an application may move between these statuses.

    Args:
        current: The application's present status.
        target: Where it is being moved to.

    Raises:
        IllegalTransitionError: If the move is not in the transition table.
    """
    if target in ALLOWED_APPLICATION_TRANSITIONS.get(current, frozenset()):
        return
    raise IllegalTransitionError(
        f"An application cannot move from {current.value} to {target.value}."
    )


def ensure_invitation_transition_allowed(
    current: InvitationStatus, target: InvitationStatus
) -> None:
    """Raise unless an invitation may move between these statuses.

    Raises:
        IllegalTransitionError: If the move is not in the transition table.
    """
    if target in ALLOWED_INVITATION_TRANSITIONS.get(current, frozenset()):
        return
    raise IllegalTransitionError(
        f"An invitation cannot move from {current.value} to {target.value}."
    )


def ensure_application_may_be_submitted(
    animal_status: AnimalStatus,
    adopter_profile_is_complete: bool,
    existing_active_application_ids: list[str],
) -> None:
    """Raise unless a new application is permitted.

    Args:
        animal_status: The animal's current status.
        adopter_profile_is_complete: Whether the adopter finished their profile.
        existing_active_application_ids: The adopter's active applications for
            *this same animal*.

    Raises:
        ApplicationNotAllowedError: If any precondition fails.
    """
    if not animal_status.is_open_for_applications:
        raise ApplicationNotAllowedError(
            f"This animal is {animal_status.value.replace('_', ' ').lower()} "
            f"and is not accepting applications."
        )

    if not adopter_profile_is_complete:
        raise ApplicationNotAllowedError(
            "Please complete your adoption profile before applying."
        )

    if existing_active_application_ids:
        raise ApplicationNotAllowedError(
            "You already have an active application for this animal."
        )


def ensure_application_may_be_approved(
    application_status: ApplicationStatus, animal_status: AnimalStatus
) -> None:
    """Raise unless this application may be approved right now.

    The status transition is not the whole rule. An animal already promised
    to somebody is the case that matters: two staff members looking at two
    different applications for the same animal would each see a perfectly
    approvable application, and approving both promises one animal to two
    homes - with two adopters told they had succeeded.

    The animal's status is the shared fact that settles it, because the
    first approval moves the animal out of AVAILABLE.

    Args:
        application_status: The application's present status.
        animal_status: The status of the animal it is for.

    Raises:
        ApplicationNotAllowedError: The animal is no longer available.
        IllegalTransitionError: The application cannot be approved from its
            current status.
    """
    ensure_application_transition_allowed(
        application_status, ApplicationStatus.APPROVED
    )

    if animal_status is not AnimalStatus.AVAILABLE:
        raise ApplicationNotAllowedError(
            f"This animal is {animal_status.value.replace('_', ' ').lower()}. "
            f"Reverse the existing approval first if this is a correction."
        )


def ensure_approval_may_be_reversed(application_status: ApplicationStatus) -> None:
    """Raise unless this application is one whose approval can be undone.

    Guarding on the transition to WITHDRAWN was not enough: SUBMITTED and
    UNDER_REVIEW both permit WITHDRAWN, so a never-approved application
    passed. The damage was the side effect rather than the status change -
    the handler then forced the animal back to AVAILABLE, discarding an
    ADOPTION_IN_PROGRESS that a *different* adopter's genuine approval had
    set.

    Args:
        application_status: The application's present status.

    Raises:
        IllegalTransitionError: It was not in an approved state.
    """
    if application_status is not ApplicationStatus.APPROVED:
        raise IllegalTransitionError(
            f"Only an approved application can be reversed; this one is "
            f"{application_status.value.replace('_', ' ').lower()}."
        )


def select_applications_to_close(
    approved_application_id: str, adopter_applications: list[ApplicationSnapshot]
) -> list[ApplicationSnapshot]:
    """Choose which of an adopter's applications the approval closes.

    Spec section 7.5: approving one application closes the adopter's other
    *active* applications. Applications already final are untouched, and the
    approved application itself is obviously excluded.

    Args:
        approved_application_id: The application that was just approved.
        adopter_applications: Every application belonging to that adopter.

    Returns:
        The applications to close, in a stable order.
    """
    return sorted(
        (
            application
            for application in adopter_applications
            if application.application_id != approved_application_id
            and application.status.is_active
        ),
        key=lambda application: application.application_id,
    )


def may_reopen(
    application: ApplicationSnapshot, reversed_application_id: str
) -> bool:
    """Whether reversing one approval makes this closed application reopenable.

    Reaching SUBMITTED again is structurally legal from CLOSED, but that is
    not the whole rule. An application the adopter withdrew, or that staff
    rejected on its own merits, is also CLOSED - and reopening it would
    resurrect a decision nobody reversed. The cause recorded on the closing
    event is what tells those cases apart.

    Args:
        application: The application to judge.
        reversed_application_id: The approval that was undone.

    Returns:
        True only when this application was closed by that approval.
    """
    return (
        application.status is ApplicationStatus.CLOSED
        and application.closed_because_application_id == reversed_application_id
    )


def select_applications_to_reopen(
    reversed_application_id: str, adopter_applications: list[ApplicationSnapshot]
) -> list[ApplicationSnapshot]:
    """Choose which applications a reversed approval makes reopenable.

    This is the rule that justifies event sourcing for this project. When an
    approval is undone, only the applications closed *because of that specific
    approval* may reopen. An application the adopter withdrew, or that staff
    rejected on its own merits, must stay closed - reopening it would
    resurrect a decision nobody reversed.

    A current-state-only schema cannot tell those cases apart, because
    `status = CLOSED` looks identical in both. The causing identifier is
    recorded on the closing event and projected onto the row, which turns the
    question from a guess into a lookup.

    Args:
        reversed_application_id: The approval that was undone.
        adopter_applications: Every application belonging to that adopter.

    Returns:
        The applications eligible to reopen, in a stable order.
    """
    return sorted(
        (
            application
            for application in adopter_applications
            if may_reopen(application, reversed_application_id)
        ),
        key=lambda application: application.application_id,
    )


def animal_status_after_approval() -> AnimalStatus:
    """The status an animal takes when one of its applications is approved."""
    return AnimalStatus.ADOPTION_IN_PROGRESS


def animal_status_after_reversal() -> AnimalStatus:
    """The status an animal returns to when an approval is undone."""
    return AnimalStatus.AVAILABLE
