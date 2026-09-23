"""Unit tests for invitation rules, especially the 72-hour window.

The expiry boundary is the part most likely to be quietly wrong, so it is
tested from both sides rather than only in the middle. These run with no
database and no clock dependency: `now` is always passed in explicitly, which
is what makes the boundary testable at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.domain.enums import AnimalStatus, InvitationStatus
from app.domain.invitation_rules import (
    INVITATION_RESPONSE_WINDOW,
    InvitationEligibility,
    InvitationExpiredError,
    InvitationNotAllowedError,
    calculate_expiry,
    ensure_invitation_may_be_sent,
    ensure_response_is_allowed,
    has_expired,
    status_after_expiry_sweep,
)

pytestmark = pytest.mark.unit

SENT_AT = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)


def eligibility(**overrides: object) -> InvitationEligibility:
    """Build an eligible candidate, overriding only the rule under test."""
    defaults: dict[str, object] = {
        "adopter_profile_is_complete": True,
        "adopter_opted_in": True,
        "adopter_account_is_active": True,
        "animal_status": AnimalStatus.AVAILABLE,
        "has_open_invitation_for_this_animal": False,
        "has_active_application_for_this_animal": False,
    }
    defaults.update(overrides)
    return InvitationEligibility(**defaults)  # type: ignore[arg-type]


class TestExpiryWindow:
    """Spec section 7.4 fixes the window at exactly 72 hours."""

    def test_window_is_seventy_two_hours(self) -> None:
        """Proves the constant matches the specification."""
        assert timedelta(hours=72) == INVITATION_RESPONSE_WINDOW

    def test_expiry_is_exactly_seventy_two_hours_after_sending(self) -> None:
        """Proves the arithmetic, not just the constant."""
        assert calculate_expiry(SENT_AT) == datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)

    def test_naive_timestamp_is_rejected(self) -> None:
        """Proves a naive datetime cannot silently produce a wrong window.

        A naive value would compute an expiry in an unknown timezone, which
        could be hours out. Failing loudly is the only safe behaviour, and is
        why ruff's DTZ rules are enabled project-wide.
        """
        with pytest.raises(ValueError, match="timezone-aware"):
            calculate_expiry(datetime(2026, 9, 23, 12, 0, 0))  # noqa: DTZ001

    def test_not_expired_one_second_before_the_boundary(self) -> None:
        """Proves the window is still open right up to the end."""
        expires_at = calculate_expiry(SENT_AT)
        assert not has_expired(expires_at, expires_at - timedelta(seconds=1))

    def test_not_expired_exactly_at_the_boundary(self) -> None:
        """Proves the boundary instant still counts as open.

        An adopter replying at the exact deadline should be honoured rather
        than rejected by an off-by-one.
        """
        expires_at = calculate_expiry(SENT_AT)
        assert not has_expired(expires_at, expires_at)

    def test_expired_one_second_after_the_boundary(self) -> None:
        """Proves the window really does close."""
        expires_at = calculate_expiry(SENT_AT)
        assert has_expired(expires_at, expires_at + timedelta(seconds=1))


class TestSendingEligibility:
    """Spec section 10 eligibility, applied before an invitation is sent."""

    def test_eligible_adopter_passes(self) -> None:
        """Proves a fully eligible candidate is allowed."""
        ensure_invitation_may_be_sent(eligibility())

    def test_incomplete_profile_refused(self) -> None:
        """Proves an incomplete profile blocks proactive outreach."""
        with pytest.raises(InvitationNotAllowedError, match="profile"):
            ensure_invitation_may_be_sent(eligibility(adopter_profile_is_complete=False))

    def test_opted_out_adopter_refused(self) -> None:
        """Proves the opt-in of spec 5.1 is genuinely enforced.

        This is the rule the whole proactive-discovery feature rests on:
        somebody who never agreed to be contacted must never be contacted.
        """
        with pytest.raises(InvitationNotAllowedError, match="opted in"):
            ensure_invitation_may_be_sent(eligibility(adopter_opted_in=False))

    def test_inactive_account_refused(self) -> None:
        """Proves a deactivated account receives no invitations."""
        with pytest.raises(InvitationNotAllowedError, match="not active"):
            ensure_invitation_may_be_sent(eligibility(adopter_account_is_active=False))

    @pytest.mark.parametrize(
        "status",
        [
            AnimalStatus.RESERVED,
            AnimalStatus.ADOPTION_IN_PROGRESS,
            AnimalStatus.ADOPTED,
            AnimalStatus.UNAVAILABLE,
        ],
    )
    def test_unavailable_animal_refused(self, status: AnimalStatus) -> None:
        """Proves no animal outside AVAILABLE can be offered."""
        with pytest.raises(InvitationNotAllowedError):
            ensure_invitation_may_be_sent(eligibility(animal_status=status))

    def test_duplicate_open_invitation_refused(self) -> None:
        """Proves an adopter is not invited twice for the same animal."""
        with pytest.raises(InvitationNotAllowedError, match="already has an open"):
            ensure_invitation_may_be_sent(
                eligibility(has_open_invitation_for_this_animal=True)
            )

    def test_existing_applicant_refused(self) -> None:
        """Proves somebody already applying is not invited to apply.

        They are already in the workflow the invitation would start.
        """
        with pytest.raises(InvitationNotAllowedError, match="already applied"):
            ensure_invitation_may_be_sent(
                eligibility(has_active_application_for_this_animal=True)
            )


class TestResponding:
    """Who may answer, and when."""

    def test_sent_invitation_may_be_answered(self) -> None:
        """Proves a fresh invitation is answerable."""
        ensure_response_is_allowed(
            InvitationStatus.SENT, calculate_expiry(SENT_AT), SENT_AT + timedelta(hours=1)
        )

    def test_viewed_invitation_may_be_answered(self) -> None:
        """Proves opening an invitation does not consume the right to reply."""
        ensure_response_is_allowed(
            InvitationStatus.VIEWED, calculate_expiry(SENT_AT), SENT_AT + timedelta(hours=1)
        )

    def test_response_after_the_window_is_refused(self) -> None:
        """Proves a late reply is rejected."""
        with pytest.raises(InvitationExpiredError, match="72 hours"):
            ensure_response_is_allowed(
                InvitationStatus.SENT,
                calculate_expiry(SENT_AT),
                SENT_AT + timedelta(hours=73),
            )

    @pytest.mark.parametrize(
        "status",
        [InvitationStatus.ACCEPTED, InvitationStatus.DECLINED, InvitationStatus.EXPIRED],
    )
    def test_already_answered_invitation_is_refused(
        self, status: InvitationStatus
    ) -> None:
        """Proves an invitation cannot be answered twice."""
        with pytest.raises(InvitationNotAllowedError, match="already"):
            ensure_response_is_allowed(
                status, calculate_expiry(SENT_AT), SENT_AT + timedelta(hours=1)
            )


class TestExpirySweep:
    """The sweep expires only what should be expired."""

    def test_overdue_unanswered_invitation_is_expired(self) -> None:
        """Proves the sweep catches an ignored invitation."""
        result = status_after_expiry_sweep(
            InvitationStatus.SENT, calculate_expiry(SENT_AT), SENT_AT + timedelta(hours=80)
        )
        assert result is InvitationStatus.EXPIRED

    def test_invitation_inside_its_window_is_left_alone(self) -> None:
        """Proves the sweep does not expire live invitations."""
        result = status_after_expiry_sweep(
            InvitationStatus.SENT, calculate_expiry(SENT_AT), SENT_AT + timedelta(hours=10)
        )
        assert result is None

    def test_accepted_invitation_never_expires(self) -> None:
        """Proves an answered invitation is untouched however long ago it was.

        Without this, a sweep run later would overwrite a recorded acceptance
        with EXPIRED and lose the adopter's answer.
        """
        result = status_after_expiry_sweep(
            InvitationStatus.ACCEPTED,
            calculate_expiry(SENT_AT),
            SENT_AT + timedelta(days=90),
        )
        assert result is None

    def test_declined_invitation_never_expires(self) -> None:
        """Proves a decline is equally permanent."""
        result = status_after_expiry_sweep(
            InvitationStatus.DECLINED,
            calculate_expiry(SENT_AT),
            SENT_AT + timedelta(days=90),
        )
        assert result is None

    def test_sweep_is_idempotent(self) -> None:
        """Proves running the sweep twice does not re-expire anything.

        A second run must produce no further events, or the history would
        fill with duplicate expiries every time the sweep ran.
        """
        already_expired = status_after_expiry_sweep(
            InvitationStatus.EXPIRED,
            calculate_expiry(SENT_AT),
            SENT_AT + timedelta(hours=100),
        )
        assert already_expired is None
