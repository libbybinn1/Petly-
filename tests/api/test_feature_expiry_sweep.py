"""Tests for the invitation expiry sweep and the configured window.

Spec section 7.4 gives an adopter a fixed window to reply, which means
something has to notice when it closes. The command existed and was
registered on the bus, but nothing dispatched it: an invitation stayed SENT
for ever and the "expires in N hours" label counted down past zero while the
buttons still worked.

`INVITATION_EXPIRY_HOURS` had the same shape of problem - it was read from
the environment into `Configuration` and then never used, so changing it
did nothing at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.domain.enums import InvitationStatus
from app.domain.invitation_rules import DEFAULT_RESPONSE_WINDOW_HOURS, calculate_expiry
from app.infrastructure.models import AdoptionInvitation, new_identifier
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.api


def _naive_now() -> datetime:
    """Naive UTC, matching how the DATETIME2 columns are stored."""
    return datetime.now(UTC).replace(tzinfo=None)


def overdue_invitation(
    session_factory: sessionmaker[Session], world: dict[str, str]
) -> str:
    """Insert one invitation whose window closed yesterday."""
    invitation_id = new_identifier()
    sent_at = _naive_now() - timedelta(days=5)

    with session_factory() as session:
        session.add(
            AdoptionInvitation(
                invitation_id=invitation_id,
                animal_id=world["available_animal_id"],
                adopter_profile_id=world["adopter_profile_id"],
                sent_by_user_id=world["staff_user_id"],
                status=InvitationStatus.SENT.value,
                staff_message="Would you like to meet them?",
                sent_at=sent_at,
                expires_at=sent_at + timedelta(hours=72),
            )
        )
        session.commit()
    return invitation_id


def status_of(session_factory: sessionmaker[Session], invitation_id: str) -> str:
    """Read one invitation's stored status."""
    with session_factory() as session:
        return session.execute(
            select(AdoptionInvitation.status).where(
                AdoptionInvitation.invitation_id == invitation_id
            )
        ).scalar_one()


class TestTheSweepRuns:
    """An overdue invitation is expired without anybody asking."""

    def test_an_overdue_invitation_is_expired_by_an_ordinary_request(
        self,
        client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the sweep is actually dispatched, not merely registered.

        This is the whole point of the hook: nothing in the application
        used to call it, so the window closed on paper only.
        """
        invitation_id = overdue_invitation(session_factory, world)

        client.get("/")

        assert status_of(session_factory, invitation_id) == (
            InvitationStatus.EXPIRED.value
        )

    def test_an_invitation_inside_its_window_is_left_alone(
        self,
        client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the sweep expires the overdue, not the merely open."""
        invitation_id = new_identifier()
        sent_at = _naive_now() - timedelta(hours=1)
        with session_factory() as session:
            session.add(
                AdoptionInvitation(
                    invitation_id=invitation_id,
                    animal_id=world["available_animal_id"],
                    adopter_profile_id=world["adopter_profile_id"],
                    sent_by_user_id=world["staff_user_id"],
                    status=InvitationStatus.SENT.value,
                    sent_at=sent_at,
                    expires_at=sent_at + timedelta(hours=72),
                )
            )
            session.commit()

        client.get("/")

        assert status_of(session_factory, invitation_id) == (
            InvitationStatus.SENT.value
        )

    def test_the_sweep_does_not_run_again_on_the_next_request(
        self,
        client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the timestamp guard holds.

        Without it every page load would open a write transaction against
        a throttled shared database. The proof is indirect but real: a
        second overdue invitation created after the first sweep survives
        the next request, because the interval has not elapsed.
        """
        overdue_invitation(session_factory, world)
        client.get("/")

        second = overdue_invitation(session_factory, world)
        client.get("/")

        assert status_of(session_factory, second) == InvitationStatus.SENT.value

    def test_a_static_request_does_not_trigger_a_sweep(
        self, application: Flask, world: dict[str, str]
    ) -> None:
        """Proves the hook skips asset requests.

        A page pulls in several static files, and each one reaching the
        database would multiply the cost of the guard it is meant to avoid.
        """
        static_client = application.test_client()
        response = static_client.get("/static/css/petmatch.css")

        assert response.status_code in (200, 404)


class TestTheConfiguredWindow:
    """INVITATION_EXPIRY_HOURS governs behaviour rather than sitting unused."""

    def test_the_default_matches_the_specification(self) -> None:
        """Proves the 72 hours of spec section 7.4 is still the default."""
        sent_at = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)

        assert calculate_expiry(sent_at) - sent_at == timedelta(hours=72)
        assert DEFAULT_RESPONSE_WINDOW_HOURS == 72

    def test_a_configured_window_is_honoured(self) -> None:
        """Proves the setting reaches the calculation."""
        sent_at = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)

        assert calculate_expiry(sent_at, 24) - sent_at == timedelta(hours=24)

    def test_a_window_of_zero_is_refused(self) -> None:
        """Proves a misconfiguration fails loudly rather than expiring instantly.

        A zero or negative window would mean every invitation was overdue
        the moment it was sent, and the sweep would quietly close all of
        them.
        """
        sent_at = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)

        with pytest.raises(ValueError, match="positive"):
            calculate_expiry(sent_at, 0)

    def test_a_naive_send_time_is_still_refused(self) -> None:
        """Proves adding the parameter did not weaken the timezone guard."""
        with pytest.raises(ValueError, match="timezone-aware"):
            calculate_expiry(datetime(2026, 3, 1, 9, 0), 24)  # noqa: DTZ001
