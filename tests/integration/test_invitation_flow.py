"""Integration tests for the invitation lifecycle.

Two things matter most here and both are tested against a real database:
that accepting an invitation *starts an application rather than approving an
adoption* (spec 7.4), and that one adopter cannot act on another adopter's
records (FR-2.4).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from app.cqrs.commands.application_commands import (
    NotYourRecordError,
    WithdrawApplicationCommand,
    WithdrawApplicationHandler,
)
from app.cqrs.commands.invitation_commands import (
    ExpireOverdueInvitationsCommand,
    ExpireOverdueInvitationsHandler,
    MarkInvitationViewedCommand,
    MarkInvitationViewedHandler,
    RespondToInvitationCommand,
    RespondToInvitationHandler,
    SendInvitationCommand,
    SendInvitationHandler,
)
from app.domain.enums import (
    ActivityLevel,
    AnimalSize,
    AnimalStatus,
    ApplicationStatus,
    DomainEventType,
    ExperienceLevel,
    HomeType,
    InvitationStatus,
    Species,
    Temperament,
    UserRole,
)
from app.domain.invitation_rules import InvitationExpiredError, InvitationNotAllowedError
from app.eventstore.store import EventStore
from app.infrastructure.database import Base
from app.infrastructure.models import (
    AdopterProfile,
    AdoptionApplication,
    AdoptionInvitation,
    Animal,
    Notification,
    User,
    new_identifier,
)
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration


def _now() -> datetime:
    """Naive UTC, matching storage."""
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture
def session_factory() -> Iterator[sessionmaker[Session]]:
    """An empty in-memory database with the real schema."""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, future=True)
    engine.dispose()


def _make_adopter(session: Session, email: str, *, opted_in: bool = True,
                  complete: bool = True, active: bool = True) -> tuple[str, str]:
    """Create one adopter, returning (user_id, adopter_profile_id)."""
    user = User(
        user_id=new_identifier(), email=email, password_hash="x",
        full_name=email.split("@")[0].title(), role=UserRole.ADOPTER.value,
        is_active=active, created_at=_now(),
    )
    profile = AdopterProfile(
        adopter_profile_id=new_identifier(), user_id=user.user_id,
        home_type=HomeType.HOUSE.value, has_yard=True,
        household_has_children=False, has_other_animals=False,
        experience_level=ExperienceLevel.SOME.value,
        activity_level=ActivityLevel.MODERATE.value,
        daily_hours_available=4.0, city="Haifa",
        open_to_proactive_suggestions=opted_in, is_complete=complete,
        created_at=_now(), updated_at=_now(),
    )
    session.add_all([user, profile])
    return user.user_id, profile.adopter_profile_id


@pytest.fixture
def world(session_factory: sessionmaker[Session]) -> dict[str, str]:
    """Two adopters, one staff member and one available animal."""
    identifiers: dict[str, str] = {}

    with session_factory() as session:
        staff = User(
            user_id=new_identifier(), email="staff@petmatch.org", password_hash="x",
            full_name="Dana", role=UserRole.STAFF.value, is_active=True,
            created_at=_now(),
        )
        session.add(staff)

        first_user, first_profile = _make_adopter(session, "first@example.com")
        second_user, second_profile = _make_adopter(session, "second@example.com")
        _, opted_out_profile = _make_adopter(
            session, "optedout@example.com", opted_in=False
        )

        animal = Animal(
            animal_id=new_identifier(), name="Clover", species=Species.RABBIT.value,
            age_years=2.0, size=AnimalSize.SMALL.value,
            temperament=Temperament.CALM.value,
            activity_level=ActivityLevel.LOW.value,
            good_with_children=True, good_with_other_animals=True,
            has_special_needs=False, required_space=AnimalSize.SMALL.value,
            city="Haifa", status=AnimalStatus.AVAILABLE.value,
            created_at=_now(), updated_at=_now(),
        )
        session.add(animal)
        session.commit()

        identifiers.update(
            staff_user_id=staff.user_id,
            first_user_id=first_user, first_profile=first_profile,
            second_user_id=second_user, second_profile=second_profile,
            opted_out_profile=opted_out_profile,
            animal_id=animal.animal_id,
        )

    return identifiers


def send_invitation(
    session_factory: sessionmaker[Session], world: dict[str, str], profile_key: str
) -> str:
    """Send one invitation and return its identifier."""
    with session_factory() as session:
        invitation_id = SendInvitationHandler().handle(
            SendInvitationCommand(
                animal_id=world["animal_id"],
                adopter_profile_id=world[profile_key],
                staff_user_id=world["staff_user_id"],
                staff_message="We thought Clover might suit you.",
            ),
            session,
        )
        session.commit()
    return invitation_id


class TestSending:
    """Eligibility is enforced when an invitation is sent."""

    def test_invitation_is_recorded_with_a_seventy_two_hour_window(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the spec 7.4 window is applied on write, not just in theory."""
        invitation_id = send_invitation(session_factory, world, "first_profile")

        with session_factory() as session:
            row = session.get(AdoptionInvitation, invitation_id)
            assert row is not None
            assert row.status == InvitationStatus.SENT.value
            assert row.expires_at - row.sent_at == timedelta(hours=72)

    def test_sending_notifies_the_adopter(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the internal inbox is populated (spec 23)."""
        send_invitation(session_factory, world, "first_profile")

        with session_factory() as session:
            notifications = session.execute(
                select(Notification).where(Notification.user_id == world["first_user_id"])
            ).scalars().all()
            assert len(notifications) == 1
            assert not notifications[0].is_read

    def test_opted_out_adopter_cannot_be_invited(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the opt-in of spec 5.1 is enforced at the database boundary.

        This is the rule proactive discovery rests on: somebody who never
        agreed to be contacted must never be contacted.
        """
        with pytest.raises(InvitationNotAllowedError, match="opted in"), \
                session_factory() as session:
            SendInvitationHandler().handle(
                SendInvitationCommand(
                    animal_id=world["animal_id"],
                    adopter_profile_id=world["opted_out_profile"],
                    staff_user_id=world["staff_user_id"],
                ),
                session,
            )

    def test_cannot_send_two_open_invitations_for_one_animal(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves an adopter is not pestered twice about the same animal."""
        send_invitation(session_factory, world, "first_profile")

        with pytest.raises(InvitationNotAllowedError, match="already has an open"), \
                session_factory() as session:
            SendInvitationHandler().handle(
                SendInvitationCommand(
                    animal_id=world["animal_id"],
                    adopter_profile_id=world["first_profile"],
                    staff_user_id=world["staff_user_id"],
                ),
                session,
            )


class TestResponding:
    """Accepting starts an application; it does not approve an adoption."""

    def test_accepting_creates_an_application_not_an_approval(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves spec 7.4, the distinction the whole feature turns on.

        An accepted invitation must leave the adopter at the *start* of the
        review process, not the end of it. If this ever produced an APPROVED
        application, the system would be approving adoptions without a human
        decision, violating rule R4.
        """
        invitation_id = send_invitation(session_factory, world, "first_profile")

        with session_factory() as session:
            application_id = RespondToInvitationHandler().handle(
                RespondToInvitationCommand(
                    invitation_id=invitation_id,
                    actor_user_id=world["first_user_id"],
                    adopter_profile_id=world["first_profile"],
                    accepted=True,
                ),
                session,
            )
            session.commit()

        assert application_id is not None

        with session_factory() as session:
            application = session.get(AdoptionApplication, application_id)
            assert application is not None
            assert application.status == ApplicationStatus.SUBMITTED.value
            assert application.status != ApplicationStatus.APPROVED.value
            assert application.originating_invitation_id == invitation_id

            animal = session.get(Animal, world["animal_id"])
            assert animal is not None
            assert animal.status == AnimalStatus.AVAILABLE.value

    def test_declining_creates_no_application(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves a decline leaves no trace in the application table."""
        invitation_id = send_invitation(session_factory, world, "first_profile")

        with session_factory() as session:
            result = RespondToInvitationHandler().handle(
                RespondToInvitationCommand(
                    invitation_id=invitation_id,
                    actor_user_id=world["first_user_id"],
                    adopter_profile_id=world["first_profile"],
                    accepted=False,
                ),
                session,
            )
            session.commit()

        assert result is None
        with session_factory() as session:
            assert session.execute(select(AdoptionApplication)).first() is None

    def test_cannot_respond_twice(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves an answered invitation is final."""
        invitation_id = send_invitation(session_factory, world, "first_profile")

        with session_factory() as session:
            RespondToInvitationHandler().handle(
                RespondToInvitationCommand(
                    invitation_id, world["first_user_id"], world["first_profile"], False
                ),
                session,
            )
            session.commit()

        with pytest.raises(InvitationNotAllowedError), session_factory() as session:
            RespondToInvitationHandler().handle(
                RespondToInvitationCommand(
                    invitation_id, world["first_user_id"], world["first_profile"], True
                ),
                session,
            )

    def test_cannot_respond_after_the_window_closes(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the 72-hour deadline is enforced against real stored data."""
        invitation_id = send_invitation(session_factory, world, "first_profile")

        with session_factory() as session:
            row = session.get(AdoptionInvitation, invitation_id)
            assert row is not None
            row.expires_at = _now() - timedelta(hours=1)
            session.commit()

        with pytest.raises(InvitationExpiredError), session_factory() as session:
            RespondToInvitationHandler().handle(
                RespondToInvitationCommand(
                    invitation_id, world["first_user_id"], world["first_profile"], True
                ),
                session,
            )


class TestOwnership:
    """One adopter cannot act on another adopter's records (FR-2.4)."""

    def test_cannot_respond_to_another_adopters_invitation(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves guessing an invitation identifier achieves nothing.

        The identifier comes from the URL on this route, so without this
        check any adopter could accept or decline on somebody else's behalf.
        """
        invitation_id = send_invitation(session_factory, world, "first_profile")

        with pytest.raises(NotYourRecordError), session_factory() as session:
            RespondToInvitationHandler().handle(
                RespondToInvitationCommand(
                    invitation_id=invitation_id,
                    actor_user_id=world["second_user_id"],
                    adopter_profile_id=world["second_profile"],
                    accepted=True,
                ),
                session,
            )

    def test_cannot_mark_another_adopters_invitation_viewed(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves even the harmless-looking view command checks ownership."""
        invitation_id = send_invitation(session_factory, world, "first_profile")

        with pytest.raises(NotYourRecordError), session_factory() as session:
            MarkInvitationViewedHandler().handle(
                MarkInvitationViewedCommand(
                    invitation_id=invitation_id,
                    actor_user_id=world["second_user_id"],
                    adopter_profile_id=world["second_profile"],
                ),
                session,
            )

    def test_cannot_withdraw_another_adopters_application(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves an adopter cannot withdraw somebody else's application."""
        invitation_id = send_invitation(session_factory, world, "first_profile")

        with session_factory() as session:
            application_id = RespondToInvitationHandler().handle(
                RespondToInvitationCommand(
                    invitation_id, world["first_user_id"], world["first_profile"], True
                ),
                session,
            )
            session.commit()

        with pytest.raises(NotYourRecordError), session_factory() as session:
            WithdrawApplicationHandler().handle(
                WithdrawApplicationCommand(
                    application_id=str(application_id),
                    actor_user_id=world["second_user_id"],
                    adopter_profile_id=world["second_profile"],
                ),
                session,
            )

    def test_the_owner_can_still_act(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the ownership check does not block the legitimate adopter.

        A check that rejected everybody would pass the tests above while
        breaking the feature.
        """
        invitation_id = send_invitation(session_factory, world, "first_profile")

        with session_factory() as session:
            MarkInvitationViewedHandler().handle(
                MarkInvitationViewedCommand(
                    invitation_id, world["first_user_id"], world["first_profile"]
                ),
                session,
            )
            session.commit()

        with session_factory() as session:
            row = session.get(AdoptionInvitation, invitation_id)
            assert row is not None
            assert row.status == InvitationStatus.VIEWED.value


class TestExpirySweep:
    """The sweep records expiry as a real event."""

    def test_overdue_invitation_is_expired_with_an_event(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves expiry is recorded rather than merely inferred at read time.

        Without an event the history would show an invitation that quietly
        stopped mattering, with no record of when.
        """
        invitation_id = send_invitation(session_factory, world, "first_profile")

        with session_factory() as session:
            row = session.get(AdoptionInvitation, invitation_id)
            assert row is not None
            row.expires_at = _now() - timedelta(hours=1)
            session.commit()

        with session_factory() as session:
            expired = ExpireOverdueInvitationsHandler().handle(
                ExpireOverdueInvitationsCommand(), session
            )
            session.commit()

        assert expired == 1
        with session_factory() as session:
            row = session.get(AdoptionInvitation, invitation_id)
            assert row is not None
            assert row.status == InvitationStatus.EXPIRED.value

            history = [
                event.event_type
                for event in EventStore(session).read_aggregate_stream(invitation_id)
            ]
            assert DomainEventType.INVITATION_EXPIRED in history

    def test_sweep_does_not_touch_an_accepted_invitation(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves a recorded answer is never overwritten by a later sweep."""
        invitation_id = send_invitation(session_factory, world, "first_profile")

        with session_factory() as session:
            RespondToInvitationHandler().handle(
                RespondToInvitationCommand(
                    invitation_id, world["first_user_id"], world["first_profile"], True
                ),
                session,
            )
            row = session.get(AdoptionInvitation, invitation_id)
            assert row is not None
            row.expires_at = _now() - timedelta(days=10)
            session.commit()

        with session_factory() as session:
            expired = ExpireOverdueInvitationsHandler().handle(
                ExpireOverdueInvitationsCommand(), session
            )
            session.commit()

        assert expired == 0
        with session_factory() as session:
            row = session.get(AdoptionInvitation, invitation_id)
            assert row is not None
            assert row.status == InvitationStatus.ACCEPTED.value

    def test_sweep_is_idempotent(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves running the sweep repeatedly does not duplicate events."""
        invitation_id = send_invitation(session_factory, world, "first_profile")

        with session_factory() as session:
            row = session.get(AdoptionInvitation, invitation_id)
            assert row is not None
            row.expires_at = _now() - timedelta(hours=1)
            session.commit()

        for _ in range(3):
            with session_factory() as session:
                ExpireOverdueInvitationsHandler().handle(
                    ExpireOverdueInvitationsCommand(), session
                )
                session.commit()

        with session_factory() as session:
            expiry_events = [
                event
                for event in EventStore(session).read_aggregate_stream(invitation_id)
                if event.event_type is DomainEventType.INVITATION_EXPIRED
            ]
            assert len(expiry_events) == 1
