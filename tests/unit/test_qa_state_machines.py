"""Adversarial unit tests for the two state machines (spec section 25).

Written from a hostile reading of `app/domain/application_rules.py` and
`app/domain/invitation_rules.py`. The happy paths are already covered
elsewhere; everything here is an illegal move, a boundary instant or a
malformed value that a caller could plausibly hand the rules.

Tests marked `xfail(strict=True)` document a defect: they fail against the
code as it stands and will flip to XPASS the moment it is fixed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.domain.application_rules import (
    ALLOWED_APPLICATION_TRANSITIONS,
    ALLOWED_INVITATION_TRANSITIONS,
    ApplicationSnapshot,
    IllegalTransitionError,
    ensure_application_transition_allowed,
    ensure_invitation_transition_allowed,
    may_reopen,
    select_applications_to_reopen,
)
from app.domain.enums import ApplicationStatus, InvitationStatus
from app.domain.invitation_rules import (
    INVITATION_RESPONSE_WINDOW,
    InvitationExpiredError,
    InvitationNotAllowedError,
    calculate_expiry,
    ensure_response_is_allowed,
    has_expired,
    status_after_expiry_sweep,
)

pytestmark = pytest.mark.unit

SENT_AT = datetime(2025, 3, 1, 12, 0, tzinfo=UTC)
EXPIRES_AT = SENT_AT + INVITATION_RESPONSE_WINDOW


def _illegal_application_moves() -> list[tuple[ApplicationStatus, ApplicationStatus]]:
    """Every application status pair the transition table does not permit."""
    return [
        (current, target)
        for current in ApplicationStatus
        for target in ApplicationStatus
        if target not in ALLOWED_APPLICATION_TRANSITIONS.get(current, frozenset())
    ]


def _illegal_invitation_moves() -> list[tuple[InvitationStatus, InvitationStatus]]:
    """Every invitation status pair the transition table does not permit."""
    return [
        (current, target)
        for current in InvitationStatus
        for target in InvitationStatus
        if target not in ALLOWED_INVITATION_TRANSITIONS.get(current, frozenset())
    ]


def _snapshot(
    application_id: str,
    status: ApplicationStatus,
    closed_because: str | None = None,
) -> ApplicationSnapshot:
    """Build one application snapshot for the reopen rules."""
    return ApplicationSnapshot(
        application_id=application_id,
        adopter_profile_id="profile-1",
        animal_id="animal-1",
        status=status,
        closed_because_application_id=closed_because,
    )


class TestEveryIllegalApplicationMoveIsRefused:
    """The transition table is the whole truth about what is legal."""

    @pytest.mark.parametrize(("current", "target"), _illegal_application_moves())
    def test_illegal_move_raises(
        self, current: ApplicationStatus, target: ApplicationStatus
    ) -> None:
        """Proves the guard refuses every pair absent from the table, exhaustively.

        Enumerated rather than hand-picked so a transition added to the table
        without thought shows up here as a test that stopped being illegal.
        """
        with pytest.raises(IllegalTransitionError):
            ensure_application_transition_allowed(current, target)

    def test_an_approved_application_cannot_be_approved_again(self) -> None:
        """Proves a double-click on Approve is refused rather than re-approving."""
        with pytest.raises(IllegalTransitionError):
            ensure_application_transition_allowed(
                ApplicationStatus.APPROVED, ApplicationStatus.APPROVED
            )

    def test_a_rejected_application_cannot_be_withdrawn(self) -> None:
        """Proves an adopter cannot withdraw a decision staff already made."""
        with pytest.raises(IllegalTransitionError):
            ensure_application_transition_allowed(
                ApplicationStatus.REJECTED, ApplicationStatus.WITHDRAWN
            )

    def test_a_withdrawn_application_cannot_be_approved(self) -> None:
        """Proves staff cannot approve something the adopter already pulled out of."""
        with pytest.raises(IllegalTransitionError):
            ensure_application_transition_allowed(
                ApplicationStatus.WITHDRAWN, ApplicationStatus.APPROVED
            )

    def test_final_statuses_have_no_way_out_except_the_reopen_edge(self) -> None:
        """Proves REJECTED and WITHDRAWN are terminal, and only CLOSED may reopen."""
        assert ALLOWED_APPLICATION_TRANSITIONS[ApplicationStatus.REJECTED] == frozenset()
        assert ALLOWED_APPLICATION_TRANSITIONS[ApplicationStatus.WITHDRAWN] == frozenset()
        assert ALLOWED_APPLICATION_TRANSITIONS[ApplicationStatus.CLOSED] == frozenset(
            {ApplicationStatus.SUBMITTED}
        )


class TestReopeningIsGuardedByCause:
    """Spec section 7.5: only a cascade-closed application may reopen."""

    def test_reopen_ignores_a_closure_caused_by_a_different_approval(self) -> None:
        """Proves the reopen selection keys on the causing approval, not on CLOSED."""
        closed_by_other = ApplicationSnapshot(
            application_id="a1",
            adopter_profile_id="p1",
            animal_id="an1",
            status=ApplicationStatus.CLOSED,
            closed_because_application_id="some-other-approval",
        )

        assert select_applications_to_reopen("the-reversed-one", [closed_by_other]) == []

    def test_reopen_ignores_a_closure_with_no_recorded_cause(self) -> None:
        """Proves a CLOSED row with a null cause is never resurrected by guesswork."""
        closed_without_cause = ApplicationSnapshot(
            application_id="a1",
            adopter_profile_id="p1",
            animal_id="an1",
            status=ApplicationStatus.CLOSED,
            closed_because_application_id=None,
        )

        assert select_applications_to_reopen("the-reversed-one", [closed_without_cause]) == []

    def test_the_guard_named_by_the_transition_table_exists(self) -> None:
        """Proves the comment on CLOSED -> SUBMITTED names a real function.

        It did not: the table pointed at a `may_reopen` that was never
        written, so the only thing implementing the rule was a condition
        buried inside the selector. A promise in a comment that no caller
        can reach is not a guard.
        """
        closed_by_the_reversal = _snapshot(
            "a", ApplicationStatus.CLOSED, closed_because="the-reversed-one"
        )
        closed_for_its_own_reasons = _snapshot(
            "b", ApplicationStatus.CLOSED, closed_because=None
        )

        assert may_reopen(closed_by_the_reversal, "the-reversed-one") is True
        assert may_reopen(closed_for_its_own_reasons, "the-reversed-one") is False

    def test_a_withdrawn_application_never_reopens(self) -> None:
        """Proves the guard keys on cause, not merely on being closed."""
        withdrawn = _snapshot(
            "c", ApplicationStatus.WITHDRAWN, closed_because="the-reversed-one"
        )

        assert may_reopen(withdrawn, "the-reversed-one") is False


class TestEveryIllegalInvitationMoveIsRefused:
    """An answered or expired invitation is finished."""

    @pytest.mark.parametrize(("current", "target"), _illegal_invitation_moves())
    def test_illegal_move_raises(
        self, current: InvitationStatus, target: InvitationStatus
    ) -> None:
        """Proves the invitation guard refuses every pair absent from the table."""
        with pytest.raises(IllegalTransitionError):
            ensure_invitation_transition_allowed(current, target)

    def test_an_expired_invitation_cannot_be_accepted(self) -> None:
        """Proves expiry is terminal in the table, not merely discouraged."""
        with pytest.raises(IllegalTransitionError):
            ensure_invitation_transition_allowed(
                InvitationStatus.EXPIRED, InvitationStatus.ACCEPTED
            )

    def test_an_invitation_cannot_be_declined_twice(self) -> None:
        """Proves a repeated decline is an illegal move, not an idempotent no-op."""
        with pytest.raises(IllegalTransitionError):
            ensure_invitation_transition_allowed(
                InvitationStatus.DECLINED, InvitationStatus.DECLINED
            )

    def test_an_accepted_invitation_cannot_be_declined_afterwards(self) -> None:
        """Proves an adopter cannot reverse an acceptance by declining after it."""
        with pytest.raises(IllegalTransitionError):
            ensure_invitation_transition_allowed(
                InvitationStatus.ACCEPTED, InvitationStatus.DECLINED
            )


class TestRespondingAtTheWindowBoundary:
    """The 72-hour window's edges, to the microsecond."""

    def test_response_exactly_at_expiry_is_still_allowed(self) -> None:
        """Proves `has_expired` uses a strict `>`, so the final instant still answers."""
        ensure_response_is_allowed(InvitationStatus.SENT, EXPIRES_AT, EXPIRES_AT)

    def test_response_one_microsecond_after_expiry_is_refused(self) -> None:
        """Proves the window closes immediately after its final instant."""
        with pytest.raises(InvitationExpiredError):
            ensure_response_is_allowed(
                InvitationStatus.SENT,
                EXPIRES_AT,
                EXPIRES_AT + timedelta(microseconds=1),
            )

    @pytest.mark.parametrize(
        "answered",
        [InvitationStatus.ACCEPTED, InvitationStatus.DECLINED, InvitationStatus.EXPIRED],
    )
    def test_already_finished_invitation_is_refused_before_expiry_is_considered(
        self, answered: InvitationStatus
    ) -> None:
        """Proves status is checked first, so a stale invitation gives the right message.

        An EXPIRED invitation inside its window (possible if the sweep ran with
        a clock skew) must still be refused as answered rather than reopened.
        """
        with pytest.raises(InvitationNotAllowedError):
            ensure_response_is_allowed(answered, EXPIRES_AT, SENT_AT)

    def test_expiry_sweep_leaves_the_boundary_instant_alone(self) -> None:
        """Proves the sweep and the response guard agree on the boundary."""
        assert status_after_expiry_sweep(InvitationStatus.SENT, EXPIRES_AT, EXPIRES_AT) is None

    def test_expiry_sweep_expires_one_microsecond_later(self) -> None:
        """Proves the sweep fires as soon as the window has genuinely closed."""
        just_after = EXPIRES_AT + timedelta(microseconds=1)

        assert (
            status_after_expiry_sweep(InvitationStatus.SENT, EXPIRES_AT, just_after)
            is InvitationStatus.EXPIRED
        )


class TestNaiveDatetimesAreNotSilentlyAccepted:
    """SQL Server 2014 columns are naive, so the boundary is where bugs live."""

    def test_calculate_expiry_refuses_a_naive_sent_at(self) -> None:
        """Proves a naive timestamp cannot produce a silently wrong 72-hour window."""
        with pytest.raises(ValueError, match="timezone-aware"):
            calculate_expiry(datetime(2025, 3, 1, 12, 0))  # noqa: DTZ001

    def test_has_expired_rejects_a_naive_now(self) -> None:
        """Proves a naive argument gives a domain error, not a TypeError.

        It used to raise `TypeError: can't compare offset-naive and
        offset-aware datetimes` from inside the comparison. The same
        mistake in `calculate_expiry` had always been reported clearly;
        the two now agree.
        """
        naive_now = datetime(2025, 3, 5, 12, 0)  # noqa: DTZ001

        with pytest.raises(ValueError, match="timezone-aware"):
            has_expired(EXPIRES_AT, naive_now)

    def test_has_expired_rejects_a_naive_expiry(self) -> None:
        """Proves the check covers the argument that comes from the database.

        `expires_at` is read from a DATETIME2 column, which carries no
        offset - so this is the side a caller is actually likely to get
        wrong.
        """
        naive_expiry = datetime(2025, 3, 5, 12, 0)  # noqa: DTZ001

        with pytest.raises(ValueError, match="timezone-aware"):
            has_expired(naive_expiry, datetime(2025, 3, 5, 13, 0, tzinfo=UTC))

    def test_expiry_window_is_exactly_seventy_two_hours(self) -> None:
        """Proves the window matches spec section 7.4 to the second."""
        assert calculate_expiry(SENT_AT) - SENT_AT == timedelta(hours=72)
