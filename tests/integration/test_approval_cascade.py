"""Integration tests for the approval cascade and the spec 7.5 reopen rule.

These run against a real SQLite database rather than mocks, because the thing
under test is the interaction between the event log and the projection - and
a mock of that interaction would only prove the mock works.

SQLite is used rather than the cloud SQL Server so the suite stays fast and
runs offline. The behaviour tested here is in the domain and the event store,
neither of which depends on the dialect.
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
from app.domain.application_rules import (
    ApplicationNotAllowedError,
    IllegalTransitionError,
)
from app.domain.enums import (
    ActivityLevel,
    AnimalSize,
    AnimalStatus,
    ApplicationStatus,
    DomainEventType,
    ExperienceLevel,
    HomeType,
    Species,
    Temperament,
    UserRole,
)
from app.eventstore.projections import replay_application
from app.eventstore.store import EventStore
from app.infrastructure.database import Base
from app.infrastructure.models import (
    AdopterProfile,
    AdoptionApplication,
    AnalysisJob,
    Animal,
    User,
    new_identifier,
)
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration


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

        for index in range(4):
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


def status_of(session_factory: sessionmaker[Session], application_id: str) -> ApplicationStatus:
    """Read one application's current status."""
    with session_factory() as session:
        row = session.get(AdoptionApplication, application_id)
        assert row is not None
        return ApplicationStatus(row.status)


class TestSubmission:
    """Applications can be created, and refused when rules say so."""

    def test_submission_records_event_and_queues_analysis(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves one command writes the row, the event and the agent job together."""
        application_id = submit(session_factory, world, "animal_0")

        with session_factory() as session:
            assert session.get(AdoptionApplication, application_id) is not None

            events = EventStore(session).read_aggregate_stream(application_id)
            assert [event.event_type for event in events] == [
                DomainEventType.APPLICATION_SUBMITTED
            ]
            assert events[0].actor_user_id == world["adopter_user_id"]

            job = session.execute(
                select(AnalysisJob).where(AnalysisJob.application_id == application_id)
            ).scalar_one()
            assert job.status == "PENDING"

    def test_multiple_applications_to_different_animals_allowed(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves spec 5.3: an adopter may pursue several animals at once."""
        first = submit(session_factory, world, "animal_0")
        second = submit(session_factory, world, "animal_1")

        assert status_of(session_factory, first).is_active
        assert status_of(session_factory, second).is_active

    def test_duplicate_active_application_refused(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves an adopter cannot hold two live applications for one animal."""
        submit(session_factory, world, "animal_0")

        with pytest.raises(ApplicationNotAllowedError), session_factory() as session:
            SubmitApplicationHandler().handle(
                SubmitApplicationCommand(
                    adopter_profile_id=world["adopter_profile_id"],
                    animal_id=world["animal_0"],
                    actor_user_id=world["adopter_user_id"],
                ),
                session,
            )

    def test_application_to_unavailable_animal_refused(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves an adopted animal stops accepting applications."""
        with session_factory() as session:
            animal = session.get(Animal, world["animal_2"])
            assert animal is not None
            animal.status = AnimalStatus.ADOPTED.value
            session.commit()

        with pytest.raises(ApplicationNotAllowedError), session_factory() as session:
            SubmitApplicationHandler().handle(
                SubmitApplicationCommand(
                    adopter_profile_id=world["adopter_profile_id"],
                    animal_id=world["animal_2"],
                    actor_user_id=world["adopter_user_id"],
                ),
                session,
            )

    def test_incomplete_profile_refused(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves an incomplete profile blocks applying (feature F-07)."""
        with session_factory() as session:
            profile = session.get(AdopterProfile, world["adopter_profile_id"])
            assert profile is not None
            profile.is_complete = False
            session.commit()

        with pytest.raises(ApplicationNotAllowedError), session_factory() as session:
            SubmitApplicationHandler().handle(
                SubmitApplicationCommand(
                    adopter_profile_id=world["adopter_profile_id"],
                    animal_id=world["animal_3"],
                    actor_user_id=world["adopter_user_id"],
                ),
                session,
            )


class TestApprovalCascade:
    """Approving one application closes the adopter's other active ones."""

    def test_approval_closes_other_active_applications(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the spec 7.5 cascade runs."""
        approved = submit(session_factory, world, "animal_0")
        other_one = submit(session_factory, world, "animal_1")
        other_two = submit(session_factory, world, "animal_2")

        with session_factory() as session:
            closed_count = ApproveApplicationHandler().handle(
                ApproveApplicationCommand(approved, world["staff_user_id"]), session
            )
            session.commit()

        assert closed_count == 2
        assert status_of(session_factory, approved) is ApplicationStatus.APPROVED
        assert status_of(session_factory, other_one) is ApplicationStatus.CLOSED
        assert status_of(session_factory, other_two) is ApplicationStatus.CLOSED

    def test_closure_records_which_approval_caused_it(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves causation is recorded, not merely the CLOSED status.

        This is the fact a current-state schema loses, and the reason event
        sourcing is used for this process.
        """
        approved = submit(session_factory, world, "animal_0")
        other = submit(session_factory, world, "animal_1")

        with session_factory() as session:
            ApproveApplicationHandler().handle(
                ApproveApplicationCommand(approved, world["staff_user_id"]), session
            )
            session.commit()

        with session_factory() as session:
            row = session.get(AdoptionApplication, other)
            assert row is not None
            assert row.closed_because_application_id == approved

            events = EventStore(session).read_aggregate_stream(other)
            closure = next(
                event
                for event in events
                if event.event_type
                is DomainEventType.APPLICATION_CLOSED_DUE_TO_OTHER_APPROVAL
            )
            assert closure.payload["caused_by_application_id"] == approved

    def test_approval_moves_the_animal_forward(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the animal leaves the available pool on approval."""
        approved = submit(session_factory, world, "animal_0")

        with session_factory() as session:
            ApproveApplicationHandler().handle(
                ApproveApplicationCommand(approved, world["staff_user_id"]), session
            )
            session.commit()

        with session_factory() as session:
            animal = session.get(Animal, world["animal_0"])
            assert animal is not None
            assert animal.status == AnimalStatus.ADOPTION_IN_PROGRESS.value

    def test_the_status_event_names_the_animal_it_moved(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the cascade's own status event carries `animal_id`.

        Three places append `AnimalStatusChanged` and this one built its
        payload by hand without the identifier, so the dashboard's activity
        feed - which reads `payload["animal_id"]` to name the animal - had
        nothing to render after "changed the status of". All three now use
        `animal_status_changed_payload`.
        """
        approved = submit(session_factory, world, "animal_0")

        with session_factory() as session:
            ApproveApplicationHandler().handle(
                ApproveApplicationCommand(approved, world["staff_user_id"]), session
            )
            session.commit()

        with session_factory() as session:
            status_events = [
                event
                for event in EventStore(session).read_aggregate_stream(world["animal_0"])
                if event.event_type is DomainEventType.ANIMAL_STATUS_CHANGED
            ]

            assert status_events
            for event in status_events:
                assert event.payload["animal_id"] == world["animal_0"]
                assert event.payload["to"] == AnimalStatus.ADOPTION_IN_PROGRESS.value

    def test_nothing_is_ever_deleted(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves closed applications survive, as spec 7.5 requires."""
        approved = submit(session_factory, world, "animal_0")
        other = submit(session_factory, world, "animal_1")

        with session_factory() as session:
            ApproveApplicationHandler().handle(
                ApproveApplicationCommand(approved, world["staff_user_id"]), session
            )
            session.commit()

        with session_factory() as session:
            assert session.get(AdoptionApplication, other) is not None
            assert EventStore(session).read_aggregate_stream(other)


class TestReopenRule:
    """A reversal reopens exactly what that approval closed - and nothing else."""

    def test_reversal_reopens_cascade_closed_applications(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the applications closed by an approval come back when it is undone."""
        approved = submit(session_factory, world, "animal_0")
        other = submit(session_factory, world, "animal_1")

        with session_factory() as session:
            ApproveApplicationHandler().handle(
                ApproveApplicationCommand(approved, world["staff_user_id"]), session
            )
            session.commit()

        with session_factory() as session:
            reopened_count = ReverseApprovalHandler().handle(
                ReverseApprovalCommand(approved, world["staff_user_id"], "fell through"),
                session,
            )
            session.commit()

        assert reopened_count == 1
        assert status_of(session_factory, other) is ApplicationStatus.SUBMITTED

    def test_withdrawn_application_is_not_resurrected(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the rule that makes event sourcing necessary here.

        An application the adopter withdrew is CLOSED-adjacent in the sense
        that it is final, but nobody reversed *that* decision. Reopening it
        would resurrect a choice the adopter made for themselves.

        A schema storing only the current status cannot tell this application
        apart from one closed by the cascade. The recorded cause can.
        """
        approved = submit(session_factory, world, "animal_0")
        cascade_closed = submit(session_factory, world, "animal_1")
        withdrawn = submit(session_factory, world, "animal_2")

        with session_factory() as session:
            WithdrawApplicationHandler().handle(
                WithdrawApplicationCommand(
                    withdrawn,
                    world["adopter_user_id"],
                    world["adopter_profile_id"],
                ),
                session,
            )
            session.commit()

        with session_factory() as session:
            ApproveApplicationHandler().handle(
                ApproveApplicationCommand(approved, world["staff_user_id"]), session
            )
            session.commit()

        with session_factory() as session:
            reopened_count = ReverseApprovalHandler().handle(
                ReverseApprovalCommand(approved, world["staff_user_id"]), session
            )
            session.commit()

        assert reopened_count == 1
        assert status_of(session_factory, cascade_closed) is ApplicationStatus.SUBMITTED
        assert status_of(session_factory, withdrawn) is ApplicationStatus.WITHDRAWN

    def test_reversal_returns_the_animal_to_available(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves a fallen-through adoption puts the animal back on the roster."""
        approved = submit(session_factory, world, "animal_0")

        with session_factory() as session:
            ApproveApplicationHandler().handle(
                ApproveApplicationCommand(approved, world["staff_user_id"]), session
            )
            session.commit()

        with session_factory() as session:
            ReverseApprovalHandler().handle(
                ReverseApprovalCommand(approved, world["staff_user_id"]), session
            )
            session.commit()

        with session_factory() as session:
            animal = session.get(Animal, world["animal_0"])
            assert animal is not None
            assert animal.status == AnimalStatus.AVAILABLE.value

    def test_reopened_application_clears_its_closure_cause(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves a reopened application cannot be reopened twice by one approval."""
        approved = submit(session_factory, world, "animal_0")
        other = submit(session_factory, world, "animal_1")

        with session_factory() as session:
            ApproveApplicationHandler().handle(
                ApproveApplicationCommand(approved, world["staff_user_id"]), session
            )
            session.commit()
        with session_factory() as session:
            ReverseApprovalHandler().handle(
                ReverseApprovalCommand(approved, world["staff_user_id"]), session
            )
            session.commit()

        with session_factory() as session:
            row = session.get(AdoptionApplication, other)
            assert row is not None
            assert row.closed_because_application_id is None


class TestEventLogIntegrity:
    """The log is append-only, ordered and complete."""

    def test_full_history_is_preserved(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the sequence of what happened is recoverable, per blueprint 10."""
        approved = submit(session_factory, world, "animal_0")
        other = submit(session_factory, world, "animal_1")

        with session_factory() as session:
            ApproveApplicationHandler().handle(
                ApproveApplicationCommand(approved, world["staff_user_id"]), session
            )
            session.commit()
        with session_factory() as session:
            ReverseApprovalHandler().handle(
                ReverseApprovalCommand(approved, world["staff_user_id"]), session
            )
            session.commit()

        with session_factory() as session:
            history = [
                event.event_type
                for event in EventStore(session).read_aggregate_stream(other)
            ]

        assert history == [
            DomainEventType.APPLICATION_SUBMITTED,
            DomainEventType.APPLICATION_CLOSED_DUE_TO_OTHER_APPROVAL,
            DomainEventType.APPLICATION_REOPENED,
        ]

    def test_sequence_numbers_increment_per_aggregate(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves ordering is per aggregate, not global."""
        first = submit(session_factory, world, "animal_0")
        second = submit(session_factory, world, "animal_1")

        with session_factory() as session:
            store = EventStore(session)
            assert [e.sequence_number for e in store.read_aggregate_stream(first)] == [1]
            assert [e.sequence_number for e in store.read_aggregate_stream(second)] == [1]

    def test_status_is_derivable_by_replaying_the_log(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves current state can be rebuilt from events (blueprint 10, FR-13.3).

        Replays the log with an independent reducer and asserts it agrees
        with the stored projection. If the two ever diverge, the projection
        has drifted from its source of truth.
        """
        approved = submit(session_factory, world, "animal_0")
        cascade_closed = submit(session_factory, world, "animal_1")
        withdrawn = submit(session_factory, world, "animal_2")

        with session_factory() as session:
            WithdrawApplicationHandler().handle(
                WithdrawApplicationCommand(
                    withdrawn,
                    world["adopter_user_id"],
                    world["adopter_profile_id"],
                ),
                session,
            )
            session.commit()
        with session_factory() as session:
            ApproveApplicationHandler().handle(
                ApproveApplicationCommand(approved, world["staff_user_id"]), session
            )
            session.commit()

        with session_factory() as session:
            store = EventStore(session)
            for application_id in (approved, cascade_closed, withdrawn):
                # The production reducer, not a copy of it. This test used to
                # carry its own event-to-status table, which meant it proved
                # that *the test* could replay the log and left the
                # application unable to (BG-5).
                replayed = replay_application(store.read_aggregate_stream(application_id))

                stored = session.get(AdoptionApplication, application_id)
                assert stored is not None
                assert replayed.status is ApplicationStatus(stored.status), (
                    f"{application_id}: replay gave {replayed.status}, "
                    f"projection has {stored.status}"
                )


class TestIllegalTransitions:
    """Illegal state changes are refused."""

    def test_cannot_withdraw_a_rejected_application(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves a final status cannot be moved out of."""
        application_id = submit(session_factory, world, "animal_0")

        with session_factory() as session:
            row = session.get(AdoptionApplication, application_id)
            assert row is not None
            row.status = ApplicationStatus.REJECTED.value
            session.commit()

        with pytest.raises(IllegalTransitionError), session_factory() as session:
            WithdrawApplicationHandler().handle(
                WithdrawApplicationCommand(
                    application_id,
                    world["adopter_user_id"],
                    world["adopter_profile_id"],
                ),
                session,
            )

    def test_cannot_approve_a_withdrawn_application(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves staff cannot approve something the adopter already withdrew."""
        application_id = submit(session_factory, world, "animal_0")

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

        with pytest.raises(IllegalTransitionError), session_factory() as session:
            ApproveApplicationHandler().handle(
                ApproveApplicationCommand(application_id, world["staff_user_id"]), session
            )
