"""Integration tests for replay and the event-derived reopen rule (BG-5).

Four documents cite this file. It did not exist, and neither did the thing it
was supposed to prove: `rebuild_projections` was described in
docs/ARCHITECTURE.md section 4 and implemented nowhere, and both the cascade
and the reversal derived their decisions from the
`closed_because_application_id` column rather than from the log that justifies
event sourcing being here at all.

So these tests check the two properties the design actually rests on
(blueprint section 10, FR-13.3):

1. **The log explains the database.** Replaying every stream reproduces every
   row. If it did not, something had been written that was never recorded.
2. **The reopen rule reads the log.** Reversing an approval reopens exactly
   what that approval closed - derived from the closing events, with the
   column as a cross-check rather than as the source.

SQLite rather than the cloud database, for the same reason as the rest of this
suite: what is under test is the interaction between the log and the
projection, and neither depends on the dialect.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from app.cqrs.commands.application_commands import (
    ApproveApplicationCommand,
    ApproveApplicationHandler,
    ReverseApprovalCommand,
    ReverseApprovalHandler,
    SubmitApplicationCommand,
    SubmitApplicationHandler,
    WithdrawApplicationCommand,
    WithdrawApplicationHandler,
)
from app.domain.enums import (
    ActivityLevel,
    AggregateType,
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
from app.eventstore.projections import (
    EmptyStreamError,
    rebuild_projections,
    replay_application,
    replay_invitation,
)
from app.eventstore.store import EventStore
from app.infrastructure.database import Base
from app.infrastructure.models import (
    AdopterProfile,
    AdoptionApplication,
    AdoptionInvitation,
    Animal,
    User,
    new_identifier,
)
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration

ANIMAL_COUNT = 4


def _now() -> datetime:
    """Naive UTC, matching how the application stores timestamps."""
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture
def session_factory() -> Iterator[sessionmaker[Session]]:
    """An empty in-memory database with the real schema."""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, future=True)
    engine.dispose()


@pytest.fixture
def world(session_factory: sessionmaker[Session]) -> dict[str, str]:
    """One adopter, one staff member and four available animals."""
    identifiers: dict[str, str] = {}

    with session_factory() as session:
        adopter_user = User(
            user_id=new_identifier(), email="adopter@example.com",
            password_hash="x", full_name="Test Adopter",
            role=UserRole.ADOPTER.value, is_active=True, created_at=_now(),
        )
        staff_user = User(
            user_id=new_identifier(), email="staff@example.org",
            password_hash="x", full_name="Test Staff",
            role=UserRole.STAFF.value, is_active=True, created_at=_now(),
        )
        session.add_all([adopter_user, staff_user])

        profile = AdopterProfile(
            adopter_profile_id=new_identifier(), user_id=adopter_user.user_id,
            home_type=HomeType.HOUSE.value, has_yard=True,
            household_has_children=False, has_other_animals=False,
            experience_level=ExperienceLevel.SOME.value,
            activity_level=ActivityLevel.MODERATE.value,
            daily_hours_available=4.0, city="Haifa",
            open_to_proactive_suggestions=True, is_complete=True,
            created_at=_now(), updated_at=_now(),
        )
        session.add(profile)

        for index in range(ANIMAL_COUNT):
            animal_id = new_identifier()
            identifiers[f"animal_{index}"] = animal_id
            session.add(
                Animal(
                    animal_id=animal_id, name=f"Animal {index}",
                    species=Species.DOG.value, age_years=3.0,
                    size=AnimalSize.MEDIUM.value,
                    temperament=Temperament.BALANCED.value,
                    activity_level=ActivityLevel.MODERATE.value,
                    good_with_children=True, good_with_other_animals=True,
                    has_special_needs=False,
                    required_space=AnimalSize.MEDIUM.value, city="Haifa",
                    status=AnimalStatus.AVAILABLE.value,
                    created_at=_now(), updated_at=_now(),
                )
            )

        session.commit()
        identifiers["adopter_profile_id"] = profile.adopter_profile_id
        identifiers["adopter_user_id"] = adopter_user.user_id
        identifiers["staff_user_id"] = staff_user.user_id

    return identifiers


def submit(
    session_factory: sessionmaker[Session], world: dict[str, str], animal_key: str
) -> str:
    """Submit one application and return its identifier."""
    with session_factory() as session:
        application_id = SubmitApplicationHandler().handle(
            SubmitApplicationCommand(
                adopter_profile_id=world["adopter_profile_id"],
                animal_id=world[animal_key],
                actor_user_id=world["adopter_user_id"],
            ),
            session,
        )
        session.commit()
    return application_id


def approve(
    session_factory: sessionmaker[Session], world: dict[str, str], application_id: str
) -> int:
    """Approve one application and return how many others it closed."""
    with session_factory() as session:
        closed: int = ApproveApplicationHandler().handle(
            ApproveApplicationCommand(application_id, world["staff_user_id"]), session
        )
        session.commit()
    return closed


def reverse(
    session_factory: sessionmaker[Session], world: dict[str, str], application_id: str
) -> int:
    """Reverse one approval and return how many applications reopened."""
    with session_factory() as session:
        reopened: int = ReverseApprovalHandler().handle(
            ReverseApprovalCommand(application_id, world["staff_user_id"]), session
        )
        session.commit()
    return reopened


def withdraw(
    session_factory: sessionmaker[Session], world: dict[str, str], application_id: str
) -> None:
    """Withdraw one application as the adopter."""
    with session_factory() as session:
        WithdrawApplicationHandler().handle(
            WithdrawApplicationCommand(
                application_id,
                world["adopter_user_id"],
                world["adopter_profile_id"],
            ),
            session,
        )
        session.commit()


def status_of(session_factory: sessionmaker[Session], application_id: str) -> ApplicationStatus:
    """Read one application's stored status."""
    with session_factory() as session:
        row = session.get(AdoptionApplication, application_id)
        assert row is not None
        return ApplicationStatus(row.status)


class TestReplayingOneAggregate:
    """`replay_application` rebuilds an application from its own events."""

    def test_every_application_replays_to_its_stored_state_after_a_cascade(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the log explains the database after the most complex write.

        The approval cascade touches three applications at once in three
        different ways. If replay agrees with the projection for all of
        them, nothing in that write reached a row without also being
        recorded - which is the property the whole design rests on
        (blueprint 10, FR-13.3).
        """
        approved = submit(session_factory, world, "animal_0")
        cascade_closed = submit(session_factory, world, "animal_1")
        untouched = submit(session_factory, world, "animal_2")
        withdraw(session_factory, world, untouched)
        approve(session_factory, world, approved)

        with session_factory() as session:
            store = EventStore(session)
            for application_id in (approved, cascade_closed, untouched):
                replayed = replay_application(
                    store.read_aggregate_stream(application_id)
                )
                stored = session.get(AdoptionApplication, application_id)

                assert stored is not None
                assert replayed.application_id == application_id
                assert replayed.status is ApplicationStatus(stored.status)
                assert (
                    replayed.closed_because_application_id
                    == stored.closed_because_application_id
                )

    def test_replay_records_which_approval_closed_an_application(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the causation is recoverable from events alone.

        This is the fact a current-state schema cannot hold: CLOSED looks
        identical whether the adopter withdrew or another approval closed
        it, and only the event says which.
        """
        approved = submit(session_factory, world, "animal_0")
        closed = submit(session_factory, world, "animal_1")
        approve(session_factory, world, approved)

        with session_factory() as session:
            replayed = replay_application(
                EventStore(session).read_aggregate_stream(closed)
            )

        assert replayed.status is ApplicationStatus.CLOSED
        assert replayed.closed_because_application_id == approved

    def test_a_reopened_application_no_longer_carries_a_closure_cause(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the reopen event clears the cause during replay.

        Leaving it set would make a second reversal of the same approval
        reopen the application again.
        """
        approved = submit(session_factory, world, "animal_0")
        closed = submit(session_factory, world, "animal_1")
        approve(session_factory, world, approved)
        reverse(session_factory, world, approved)

        with session_factory() as session:
            replayed = replay_application(
                EventStore(session).read_aggregate_stream(closed)
            )

        assert replayed.status is ApplicationStatus.SUBMITTED
        assert replayed.closed_because_application_id is None

    def test_replaying_an_empty_stream_is_refused(self) -> None:
        """Proves a missing aggregate is an error, not a default-valued one.

        A negative case worth having: returning a blank projection would
        invent an application that never existed, and `rebuild_projections`
        would then cheerfully write it over a real row.
        """
        with pytest.raises(EmptyStreamError):
            replay_application([])

    def test_an_invitation_replays_to_its_stored_status(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the second aggregate type replays too.

        Built from events written directly rather than through the
        invitation commands, so the reducer is tested against the event
        vocabulary rather than against one command's happy path.
        """
        invitation_id = new_identifier()
        with session_factory() as session:
            store = EventStore(session)
            store.append(
                DomainEventType.INVITATION_SENT,
                AggregateType.INVITATION,
                invitation_id,
                payload={
                    "animal_id": world["animal_0"],
                    "adopter_profile_id": world["adopter_profile_id"],
                },
                actor_user_id=world["staff_user_id"],
            )
            store.append(
                DomainEventType.INVITATION_VIEWED,
                AggregateType.INVITATION,
                invitation_id,
                payload={"animal_id": world["animal_0"]},
                actor_user_id=world["adopter_user_id"],
            )
            store.append(
                DomainEventType.INVITATION_DECLINED,
                AggregateType.INVITATION,
                invitation_id,
                payload={"animal_id": world["animal_0"]},
                actor_user_id=world["adopter_user_id"],
            )
            session.commit()

        with session_factory() as session:
            replayed = replay_invitation(
                EventStore(session).read_aggregate_stream(invitation_id)
            )

        assert replayed.status is InvitationStatus.DECLINED
        assert replayed.animal_id == world["animal_0"]
        assert replayed.adopter_profile_id == world["adopter_profile_id"]
        assert replayed.viewed_at is not None
        assert replayed.responded_at is not None


class TestReopeningFromTheEventLog:
    """Spec 7.5, derived from events rather than from the projection."""

    def test_a_cascade_closed_application_reopens_on_reversal(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the reopen set comes from the closing events.

        The column is deliberately cleared before the reversal, leaving the
        log as the only record of why this application closed. It must still
        reopen - which is the difference between event sourcing being the
        design and being decoration.
        """
        approved = submit(session_factory, world, "animal_0")
        closed = submit(session_factory, world, "animal_1")
        approve(session_factory, world, approved)

        with session_factory() as session:
            row = session.get(AdoptionApplication, closed)
            assert row is not None
            row.closed_because_application_id = None
            session.commit()

        reopened = reverse(session_factory, world, approved)

        assert reopened == 1
        assert status_of(session_factory, closed) is ApplicationStatus.SUBMITTED

    def test_a_withdrawn_application_does_not_reopen(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves reversal undoes one decision, not every closure.

        The negative half of spec 7.5, and the reason the rule cannot be
        "reopen everything that is closed": nobody reversed the adopter's
        own withdrawal.
        """
        approved = submit(session_factory, world, "animal_0")
        withdrawn = submit(session_factory, world, "animal_1")
        withdraw(session_factory, world, withdrawn)
        approve(session_factory, world, approved)

        reverse(session_factory, world, approved)

        assert status_of(session_factory, withdrawn) is ApplicationStatus.WITHDRAWN

    def test_the_most_recent_closure_decides_what_a_reversal_reopens(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves a twice-closed application is judged by its latest closure.

        `other` is closed by the first approval, reopened when that approval
        is reversed, and closed again by a second approval - so its stream
        holds two `ApplicationClosedDueToOtherApproval` events with
        different causes. Reversing the second approval must reopen it
        exactly once. A rule that treated every historical closure event as
        current would double-count it, and one that kept the *first* cause
        would not reopen it at all.
        """
        first = submit(session_factory, world, "animal_0")
        other = submit(session_factory, world, "animal_1")
        approve(session_factory, world, first)
        reverse(session_factory, world, first)

        second = submit(session_factory, world, "animal_2")
        approve(session_factory, world, second)
        assert status_of(session_factory, other) is ApplicationStatus.CLOSED

        with session_factory() as session:
            replayed = replay_application(
                EventStore(session).read_aggregate_stream(other)
            )
        assert replayed.closed_because_application_id == second

        reopened = reverse(session_factory, world, second)

        assert reopened == 1
        assert status_of(session_factory, other) is ApplicationStatus.SUBMITTED


class TestRebuildingEveryProjection:
    """`rebuild_projections` compares the whole log with the whole database."""

    def test_a_consistent_world_reports_no_mismatches(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves a database written only through commands agrees with its log.

        The headline claim of docs/ARCHITECTURE.md section 4, checked rather
        than asserted in prose.
        """
        approved = submit(session_factory, world, "animal_0")
        submit(session_factory, world, "animal_1")
        approve(session_factory, world, approved)
        reverse(session_factory, world, approved)

        with session_factory() as session:
            report = rebuild_projections(session)

        assert report.is_consistent
        assert report.mismatches == ()
        assert report.matching == 2
        assert report.applied is False

    def test_a_corrupted_row_is_detected(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the rebuild is a real check, not a formality.

        The negative case that gives the passing one its meaning: a status
        edited behind the commands' back - a manual UPDATE, a bad migration
        - is exactly the drift this exists to find, and it must be named
        precisely enough to act on.
        """
        approved = submit(session_factory, world, "animal_0")
        approve(session_factory, world, approved)

        with session_factory() as session:
            row = session.get(AdoptionApplication, approved)
            assert row is not None
            row.status = ApplicationStatus.REJECTED.value
            session.commit()

        with session_factory() as session:
            report = rebuild_projections(session)

        assert not report.is_consistent
        assert report.mismatching == 1
        assert any(
            mismatch.field_name == "status"
            and mismatch.aggregate_id == approved
            and mismatch.replayed == ApplicationStatus.APPROVED.value
            and mismatch.stored == ApplicationStatus.REJECTED.value
            for mismatch in report.mismatches
        )

    def test_a_read_only_rebuild_changes_nothing(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the default mode is safe to run against real data.

        A comparison that quietly repaired things would be unusable as a
        diagnostic: nobody could ask "is this consistent?" without also
        deciding to change it.
        """
        approved = submit(session_factory, world, "animal_0")
        approve(session_factory, world, approved)

        with session_factory() as session:
            row = session.get(AdoptionApplication, approved)
            assert row is not None
            row.status = ApplicationStatus.REJECTED.value
            session.commit()

        with session_factory() as session:
            rebuild_projections(session)
            session.commit()

        assert status_of(session_factory, approved) is ApplicationStatus.REJECTED

    def test_applying_a_rebuild_repairs_the_row(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the repair mode writes the replayed state back.

        Inside the caller's transaction and nowhere else: this commits, the
        rebuild itself does not, so a half-applied repair cannot exist.
        """
        approved = submit(session_factory, world, "animal_0")
        approve(session_factory, world, approved)

        with session_factory() as session:
            row = session.get(AdoptionApplication, approved)
            assert row is not None
            row.status = ApplicationStatus.REJECTED.value
            session.commit()

        with session_factory() as session:
            report = rebuild_projections(session, apply=True)
            session.commit()

        assert report.applied is True
        assert status_of(session_factory, approved) is ApplicationStatus.APPROVED

        with session_factory() as session:
            assert rebuild_projections(session).is_consistent

    def test_a_stream_with_no_row_is_reported_as_missing(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves a deleted row is distinguished from a disagreeing one.

        The log is append-only, so events outlive whatever deleted the row.
        Counting that as a mismatch would say the two disagree about a
        value, when in fact one of them is not there at all.
        """
        approved = submit(session_factory, world, "animal_0")
        approve(session_factory, world, approved)

        with session_factory() as session:
            session.delete(session.get(AdoptionApplication, approved))
            session.commit()

        with session_factory() as session:
            report = rebuild_projections(session)

        assert report.missing == 1
        assert report.mismatching == 0
        assert not report.is_consistent

    def test_invitations_are_rebuilt_as_well_as_applications(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the rebuild covers both event-sourced aggregates.

        An invitation whose row says SENT while its log says EXPIRED is the
        drift spec 7.4's window makes likely, so it has to be in scope.
        """
        invitation_id = new_identifier()
        with session_factory() as session:
            store = EventStore(session)
            session.add(
                AdoptionInvitation(
                    invitation_id=invitation_id,
                    animal_id=world["animal_0"],
                    adopter_profile_id=world["adopter_profile_id"],
                    sent_by_user_id=world["staff_user_id"],
                    status=InvitationStatus.SENT.value,
                    sent_at=_now(), expires_at=_now(),
                )
            )
            store.append(
                DomainEventType.INVITATION_SENT,
                AggregateType.INVITATION,
                invitation_id,
                payload={"animal_id": world["animal_0"]},
                actor_user_id=world["staff_user_id"],
            )
            store.append(
                DomainEventType.INVITATION_EXPIRED,
                AggregateType.INVITATION,
                invitation_id,
                payload={"animal_id": world["animal_0"]},
            )
            session.commit()

        with session_factory() as session:
            report = rebuild_projections(session, apply=True)
            session.commit()

        assert report.mismatching == 1
        with session_factory() as session:
            stored = session.execute(
                select(AdoptionInvitation).where(
                    AdoptionInvitation.invitation_id == invitation_id
                )
            ).scalar_one()
            assert stored.status == InvitationStatus.EXPIRED.value
