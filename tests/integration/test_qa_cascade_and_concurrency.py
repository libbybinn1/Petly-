"""Adversarial integration tests for the write path and the read models.

Run against SQLite rather than the cloud SQL Server, for the same reason the
existing integration suite does: what is under test is the interaction of the
commands, the event log and the projections, none of which depends on the
dialect. The cloud database is a shared free tier and is not the place to
hammer out race conditions.

Where a test proves a defect it is marked `xfail(strict=True)` with the file
and line, so the suite stays green while the bug stays visible.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from app.cqrs.commands.application_commands import (
    ApproveApplicationCommand,
    ApproveApplicationHandler,
    ReverseApprovalCommand,
    ReverseApprovalHandler,
    SubmitApplicationCommand,
    SubmitApplicationHandler,
)
from app.cqrs.commands.invitation_commands import (
    RespondToInvitationCommand,
    RespondToInvitationHandler,
)
from app.cqrs.queries.dashboard_queries import (
    GetDashboardSummaryHandler,
    GetDashboardSummaryQuery,
)
from app.cqrs.queries.match_queries import FindMyPetHandler, FindMyPetQuery
from app.domain.application_rules import ApplicationNotAllowedError, IllegalTransitionError
from app.domain.enums import (
    AnimalStatus,
    ApplicationStatus,
    InvitationStatus,
    MatchDirection,
    UserRole,
)
from app.infrastructure.database import Base
from app.infrastructure.models import (
    AdopterProfile,
    AdoptionApplication,
    AdoptionInvitation,
    Animal,
    MatchAnalysis,
    User,
    new_identifier,
)
from sqlalchemy import Table, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration


def _now() -> datetime:
    """Naive UTC, matching how the application stores timestamps."""
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture
def session_factory(tmp_path: object) -> Iterator[sessionmaker[Session]]:
    """A throwaway SQLite database with the real schema."""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


def add_adopter(session: Session, email: str) -> tuple[str, str]:
    """Create an active adopter with a complete profile. Returns (user, profile)."""
    user = User(
        user_id=new_identifier(), email=email, password_hash="hash", full_name=email,
        role=UserRole.ADOPTER.value, is_active=True, created_at=_now(),
    )
    profile = AdopterProfile(
        adopter_profile_id=new_identifier(), user_id=user.user_id,
        home_type="HOUSE", has_yard=True, household_has_children=False,
        has_other_animals=False, experience_level="SOME", activity_level="MODERATE",
        daily_hours_available=4.0, city="Haifa",
        open_to_proactive_suggestions=True, is_complete=True,
        created_at=_now(), updated_at=_now(),
    )
    session.add_all([user, profile])
    return user.user_id, profile.adopter_profile_id


def add_animal(
    session: Session, name: str, status: AnimalStatus = AnimalStatus.AVAILABLE
) -> str:
    """Create one animal and return its identifier."""
    animal = Animal(
        animal_id=new_identifier(), name=name, species="DOG", age_years=3.0,
        size="SMALL", temperament="CALM", activity_level="LOW",
        good_with_children=True, good_with_other_animals=True, has_special_needs=False,
        required_space="SMALL", city="Haifa", status=status.value,
        created_at=_now(), updated_at=_now(),
    )
    session.add(animal)
    return animal.animal_id


def run_command(factory: sessionmaker[Session], handler: object, command: object) -> object:
    """Execute one command the way the bus would: one transaction, then commit."""
    with factory() as session:
        result = handler.handle(command, session)  # type: ignore[attr-defined]
        session.commit()
        return result


def status_of(factory: sessionmaker[Session], application_id: str) -> str:
    """Read one application's projected status."""
    with factory() as session:
        row = session.get(AdoptionApplication, application_id)
        assert row is not None
        return str(row.status)


def animal_status(factory: sessionmaker[Session], animal_id: str) -> str:
    """Read one animal's projected status."""
    with factory() as session:
        row = session.get(Animal, animal_id)
        assert row is not None
        return str(row.status)


@pytest.fixture
def world(session_factory: sessionmaker[Session]) -> dict[str, str]:
    """Two adopters and two animals, all eligible."""
    identifiers: dict[str, str] = {}
    with session_factory() as session:
        identifiers["user_one"], identifiers["profile_one"] = add_adopter(session, "one@t.test")
        identifiers["user_two"], identifiers["profile_two"] = add_adopter(session, "two@t.test")
        identifiers["dog"] = add_animal(session, "Rex")
        identifiers["cat"] = add_animal(session, "Milo")
        session.commit()
    return identifiers


class TestTheCascadeStaysInsideOneAdopter:
    """Spec section 7.5 closes *the adopter's* other applications, nobody else's."""

    def test_another_adopters_application_for_the_same_animal_is_untouched(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the cascade is scoped by adopter, not by animal.

        This is the obvious way to get the cascade wrong: closing every
        application for the approved animal would silently reject the rival
        applicants with no rejection decision ever being made.
        """
        mine = run_command(
            session_factory, SubmitApplicationHandler(),
            SubmitApplicationCommand(world["profile_one"], world["dog"], world["user_one"]),
        )
        theirs = run_command(
            session_factory, SubmitApplicationHandler(),
            SubmitApplicationCommand(world["profile_two"], world["dog"], world["user_two"]),
        )

        run_command(
            session_factory, ApproveApplicationHandler(),
            ApproveApplicationCommand(str(mine), "staff-1"),
        )

        assert status_of(session_factory, str(theirs)) == ApplicationStatus.SUBMITTED.value

    def test_the_adopters_own_other_application_is_closed_with_its_cause(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the closure records which approval caused it, for the reopen rule."""
        first = run_command(
            session_factory, SubmitApplicationHandler(),
            SubmitApplicationCommand(world["profile_one"], world["dog"], world["user_one"]),
        )
        second = run_command(
            session_factory, SubmitApplicationHandler(),
            SubmitApplicationCommand(world["profile_one"], world["cat"], world["user_one"]),
        )

        run_command(
            session_factory, ApproveApplicationHandler(),
            ApproveApplicationCommand(str(first), "staff-1"),
        )

        with session_factory() as session:
            closed = session.get(AdoptionApplication, str(second))
            assert closed is not None
            assert closed.status == ApplicationStatus.CLOSED.value
            assert closed.closed_because_application_id == str(first)

    def test_approval_moves_the_animal_into_adoption_in_progress(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the animal leaves AVAILABLE, so nobody else can apply for it."""
        application = run_command(
            session_factory, SubmitApplicationHandler(),
            SubmitApplicationCommand(world["profile_one"], world["dog"], world["user_one"]),
        )

        run_command(
            session_factory, ApproveApplicationHandler(),
            ApproveApplicationCommand(str(application), "staff-1"),
        )

        assert animal_status(session_factory, world["dog"]) == (
            AnimalStatus.ADOPTION_IN_PROGRESS.value
        )

    def test_one_animal_cannot_be_approved_for_two_adopters(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves an animal already in adoption cannot be approved for a second home."""
        mine = run_command(
            session_factory, SubmitApplicationHandler(),
            SubmitApplicationCommand(world["profile_one"], world["dog"], world["user_one"]),
        )
        theirs = run_command(
            session_factory, SubmitApplicationHandler(),
            SubmitApplicationCommand(world["profile_two"], world["dog"], world["user_two"]),
        )
        run_command(
            session_factory, ApproveApplicationHandler(),
            ApproveApplicationCommand(str(mine), "staff-1"),
        )

        with pytest.raises((IllegalTransitionError, ApplicationNotAllowedError)):
            run_command(
                session_factory, ApproveApplicationHandler(),
                ApproveApplicationCommand(str(theirs), "staff-2"),
            )

    def test_the_rival_application_is_left_untouched_by_the_refusal(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the refused approval changes nothing about the rival.

        Refusing is only half of it. A guard that rejected the second
        approval but had already written part of it would leave the rival
        in a state nobody chose.
        """
        mine = run_command(
            session_factory, SubmitApplicationHandler(),
            SubmitApplicationCommand(world["profile_one"], world["dog"], world["user_one"]),
        )
        theirs = run_command(
            session_factory, SubmitApplicationHandler(),
            SubmitApplicationCommand(world["profile_two"], world["dog"], world["user_two"]),
        )
        run_command(
            session_factory, ApproveApplicationHandler(),
            ApproveApplicationCommand(str(mine), "staff-1"),
        )

        with pytest.raises((IllegalTransitionError, ApplicationNotAllowedError)):
            run_command(
                session_factory, ApproveApplicationHandler(),
                ApproveApplicationCommand(str(theirs), "staff-2"),
            )

        assert status_of(session_factory, str(mine)) == ApplicationStatus.APPROVED.value
        assert status_of(session_factory, str(theirs)) == ApplicationStatus.SUBMITTED.value
        assert animal_status(session_factory, world["dog"]) == (
            AnimalStatus.ADOPTION_IN_PROGRESS.value
        )

    def test_a_rival_can_be_approved_once_the_first_approval_is_reversed(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the guard blocks a double promise, not the animal for good.

        The animal returning to AVAILABLE is what reopens the decision, so
        a staff member correcting a mistake is not stuck.
        """
        mine = run_command(
            session_factory, SubmitApplicationHandler(),
            SubmitApplicationCommand(world["profile_one"], world["dog"], world["user_one"]),
        )
        theirs = run_command(
            session_factory, SubmitApplicationHandler(),
            SubmitApplicationCommand(world["profile_two"], world["dog"], world["user_two"]),
        )
        run_command(
            session_factory, ApproveApplicationHandler(),
            ApproveApplicationCommand(str(mine), "staff-1"),
        )
        run_command(
            session_factory, ReverseApprovalHandler(),
            ReverseApprovalCommand(str(mine), "staff-1", "fell through"),
        )

        run_command(
            session_factory, ApproveApplicationHandler(),
            ApproveApplicationCommand(str(theirs), "staff-2"),
        )

        assert status_of(session_factory, str(theirs)) == ApplicationStatus.APPROVED.value


class TestReversingAnApproval:
    """A reversal must only undo something that was actually approved."""

    def test_reversal_reopens_only_what_that_approval_closed(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the reopen set is keyed on the causing approval (FR-7.7)."""
        approved = run_command(
            session_factory, SubmitApplicationHandler(),
            SubmitApplicationCommand(world["profile_one"], world["dog"], world["user_one"]),
        )
        cascaded = run_command(
            session_factory, SubmitApplicationHandler(),
            SubmitApplicationCommand(world["profile_one"], world["cat"], world["user_one"]),
        )
        run_command(
            session_factory, ApproveApplicationHandler(),
            ApproveApplicationCommand(str(approved), "staff-1"),
        )

        reopened = run_command(
            session_factory, ReverseApprovalHandler(),
            ReverseApprovalCommand(str(approved), "staff-1", "fell through"),
        )

        assert reopened == 1
        assert status_of(session_factory, str(cascaded)) == ApplicationStatus.SUBMITTED.value

    def test_reversal_refuses_an_application_that_was_never_approved(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves only an APPROVED application can have its approval reversed."""
        never_approved = run_command(
            session_factory, SubmitApplicationHandler(),
            SubmitApplicationCommand(world["profile_one"], world["dog"], world["user_one"]),
        )

        with pytest.raises(IllegalTransitionError):
            run_command(
                session_factory, ReverseApprovalHandler(),
                ReverseApprovalCommand(str(never_approved), "staff-1"),
            )

    def test_reversing_an_unapproved_application_leaves_the_animal_alone(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves a refused reversal cannot free somebody else's animal.

        This was the damaging half of the bug. The status change was
        recoverable; the side effect was not - the handler forced the
        animal back to AVAILABLE, discarding the ADOPTION_IN_PROGRESS that
        a different adopter's genuine approval had set.
        """
        winner = run_command(
            session_factory, SubmitApplicationHandler(),
            SubmitApplicationCommand(world["profile_one"], world["dog"], world["user_one"]),
        )
        loser = run_command(
            session_factory, SubmitApplicationHandler(),
            SubmitApplicationCommand(world["profile_two"], world["dog"], world["user_two"]),
        )
        run_command(
            session_factory, ApproveApplicationHandler(),
            ApproveApplicationCommand(str(winner), "staff-1"),
        )
        assert animal_status(session_factory, world["dog"]) == (
            AnimalStatus.ADOPTION_IN_PROGRESS.value
        )

        with pytest.raises(IllegalTransitionError):
            run_command(
                session_factory, ReverseApprovalHandler(),
                ReverseApprovalCommand(str(loser), "staff-2"),
            )

        assert animal_status(session_factory, world["dog"]) == (
            AnimalStatus.ADOPTION_IN_PROGRESS.value
        )
        assert status_of(session_factory, str(winner)) == ApplicationStatus.APPROVED.value
        assert status_of(session_factory, str(loser)) == ApplicationStatus.SUBMITTED.value


class TestAcceptingAnInvitation:
    """Accepting starts an application, and an application needs an available animal."""

    def _open_invitation(
        self, session_factory: sessionmaker[Session], profile_id: str, animal_id: str
    ) -> str:
        """Insert an invitation that is open for the next 72 hours."""
        with session_factory() as session:
            invitation = AdoptionInvitation(
                invitation_id=new_identifier(), animal_id=animal_id,
                adopter_profile_id=profile_id, sent_by_user_id="staff-1",
                status=InvitationStatus.SENT.value, sent_at=_now(),
                expires_at=_now() + timedelta(hours=72),
            )
            session.add(invitation)
            session.commit()
            return invitation.invitation_id

    def test_accepting_an_open_invitation_creates_an_application(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the happy path still holds before the negative cases below."""
        invitation = self._open_invitation(
            session_factory, world["profile_one"], world["dog"]
        )

        application_id = run_command(
            session_factory, RespondToInvitationHandler(),
            RespondToInvitationCommand(
                invitation, world["user_one"], world["profile_one"], accepted=True
            ),
        )

        assert application_id is not None
        assert status_of(session_factory, str(application_id)) == (
            ApplicationStatus.SUBMITTED.value
        )

    @pytest.mark.parametrize(
        "status",
        [AnimalStatus.ADOPTED, AnimalStatus.ADOPTION_IN_PROGRESS, AnimalStatus.UNAVAILABLE],
    )
    def test_accepting_is_refused_once_the_animal_is_no_longer_available(
        self, session_factory: sessionmaker[Session], world: dict[str, str],
        status: AnimalStatus,
    ) -> None:
        """Proves FR-7.3 is enforced on the invitation path, not only on direct apply."""
        invitation = self._open_invitation(
            session_factory, world["profile_one"], world["dog"]
        )
        with session_factory() as session:
            animal = session.get(Animal, world["dog"])
            assert animal is not None
            animal.status = status.value
            session.commit()

        with pytest.raises(ApplicationNotAllowedError):
            run_command(
                session_factory, RespondToInvitationHandler(),
                RespondToInvitationCommand(
                    invitation, world["user_one"], world["profile_one"], accepted=True
                ),
            )

    def test_direct_application_for_an_adopted_animal_is_refused(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the rule exists and works on the ordinary path, for contrast."""
        with session_factory() as session:
            animal = session.get(Animal, world["dog"])
            assert animal is not None
            animal.status = AnimalStatus.ADOPTED.value
            session.commit()

        with pytest.raises(ApplicationNotAllowedError):
            run_command(
                session_factory, SubmitApplicationHandler(),
                SubmitApplicationCommand(
                    world["profile_one"], world["dog"], world["user_one"]
                ),
            )

    def test_declining_creates_no_application(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves a decline is recorded without starting the adoption workflow."""
        invitation = self._open_invitation(
            session_factory, world["profile_one"], world["dog"]
        )

        result = run_command(
            session_factory, RespondToInvitationHandler(),
            RespondToInvitationCommand(
                invitation, world["user_one"], world["profile_one"], accepted=False
            ),
        )

        assert result is None
        with session_factory() as session:
            assert session.execute(select(AdoptionApplication)).scalars().all() == []


class TestDuplicateActiveApplications:
    """FR-7.2: never two active applications from one adopter for one animal."""

    def test_the_sequential_case_is_refused(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the check-then-insert guard works when the calls do not overlap."""
        run_command(
            session_factory, SubmitApplicationHandler(),
            SubmitApplicationCommand(world["profile_one"], world["dog"], world["user_one"]),
        )

        with pytest.raises(ApplicationNotAllowedError):
            run_command(
                session_factory, SubmitApplicationHandler(),
                SubmitApplicationCommand(
                    world["profile_one"], world["dog"], world["user_one"]
                ),
            )

    def test_the_database_itself_forbids_duplicate_active_applications(self) -> None:
        """Proves FR-7.2 is enforced by a constraint, not only by a handler check.

        docs/MODEL_DATA.md section 2.5 specifies a filtered unique index on
        (adopter_profile_id, animal_id) for the active statuses. It was
        missing from the model, which left the rule resting entirely on the
        check-then-insert in `SubmitApplicationHandler` - and a
        check-then-insert is not atomic, as the test below shows.
        """
        table = cast("Table", AdoptionApplication.__table__)
        unique_pairs = [
            {column.name for column in getattr(constraint, "columns", ())}
            for constraint in (*table.constraints, *table.indexes)
            if getattr(constraint, "unique", False)
        ]

        assert {"adopter_profile_id", "animal_id"} in unique_pairs

    def test_the_guard_cannot_see_a_concurrent_uncommitted_application(
        self, tmp_path: Path
    ) -> None:
        """Proves FR-7.2 rests on a read that a concurrent request cannot see.

        A file-backed database is used so each session holds its own
        connection. Session one inserts an application and does not commit;
        session two then re-runs exactly the guard
        `SubmitApplicationHandler` applies and finds nothing in its way, so it
        would go on to insert a second active application for the same pair.

        SQLite cannot finish the demonstration because it takes a global write
        lock, which serialises the two inserts. SQL Server 2014 under its
        default READ COMMITTED does not, which is why the filtered unique
        index documented in MODEL_DATA.md section 2.5 is the only real
        protection - and it is missing (see the xfail above).
        """
        from app.cqrs.commands.application_commands import _applications_of
        from app.domain.application_rules import ensure_application_may_be_submitted

        engine = create_engine(f"sqlite:///{tmp_path.as_posix()}/race.db", future=True)
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine, expire_on_commit=False)

        with factory() as setup:
            user_id, profile_id = add_adopter(setup, "racer@t.test")
            animal_id = add_animal(setup, "Rex")
            setup.commit()

        first, second = factory(), factory()
        try:
            SubmitApplicationHandler().handle(
                SubmitApplicationCommand(profile_id, animal_id, user_id), first
            )

            visible_active = [
                row.application_id
                for row in _applications_of(second, profile_id)
                if row.animal_id == animal_id
                and ApplicationStatus(row.status).is_active
            ]
            ensure_application_may_be_submitted(
                animal_status=AnimalStatus.AVAILABLE,
                adopter_profile_is_complete=True,
                existing_active_application_ids=visible_active,
            )
        finally:
            first.rollback()
            first.close()
            second.close()
            engine.dispose()

        assert visible_active == []


class TestDashboardCounts:
    """Spec section 22 figures, including the degenerate empty database."""

    def test_an_empty_database_produces_zeroes_and_no_division_error(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """Proves the dashboard renders before any animal exists.

        A percentage or an average over zero animals is the classic way this
        page crashes on a fresh deployment.
        """
        with session_factory() as session:
            summary = GetDashboardSummaryHandler().handle(
                GetDashboardSummaryQuery(), session
            )

        assert summary.total_animals == 0
        assert summary.animals_without_suitable_applicants == 0
        assert summary.recent_activity == []
        assert summary.needs_attention is False

    def test_animals_with_no_analyses_are_not_called_unsuitable(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves absence of evidence is not counted as evidence of a poor match."""
        run_command(
            session_factory, SubmitApplicationHandler(),
            SubmitApplicationCommand(world["profile_one"], world["dog"], world["user_one"]),
        )

        with session_factory() as session:
            summary = GetDashboardSummaryHandler().handle(
                GetDashboardSummaryQuery(), session
            )

        assert summary.animals_without_suitable_applicants == 0

    def test_a_non_applicants_high_score_does_not_hide_an_unsuitable_animal(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the figure counts the applicants' scores, not every stored score."""
        run_command(
            session_factory, SubmitApplicationHandler(),
            SubmitApplicationCommand(world["profile_one"], world["dog"], world["user_one"]),
        )
        with session_factory() as session:
            session.add_all(
                [
                    _analysis(world["profile_one"], world["dog"], score=10),
                    # profile_two never applied: a Find More Adopters analysis.
                    _analysis(world["profile_two"], world["dog"], score=95),
                ]
            )
            session.commit()

        with session_factory() as session:
            summary = GetDashboardSummaryHandler().handle(
                GetDashboardSummaryQuery(), session
            )

        assert summary.animals_without_suitable_applicants == 1

    def test_an_applicant_scoring_below_the_threshold_is_counted(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the figure does work when only the applicant has an analysis."""
        run_command(
            session_factory, SubmitApplicationHandler(),
            SubmitApplicationCommand(world["profile_one"], world["dog"], world["user_one"]),
        )
        with session_factory() as session:
            session.add(_analysis(world["profile_one"], world["dog"], score=10))
            session.commit()

        with session_factory() as session:
            summary = GetDashboardSummaryHandler().handle(
                GetDashboardSummaryQuery(), session
            )

        assert summary.animals_without_suitable_applicants == 1


class TestStoredAnalysesAreMatchedToTheirDirection:
    """A score computed one way must not be explained by the other way's analysis."""

    def test_find_my_pet_ignores_an_analysis_from_the_other_direction(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves a screen never pairs one direction's score with the other's prose."""
        with session_factory() as session:
            session.add(
                _analysis(
                    world["profile_one"], world["dog"], score=42,
                    direction=MatchDirection.ANIMAL_TO_ADOPTER,
                )
            )
            session.commit()

        with session_factory() as session:
            ranked = FindMyPetHandler().handle(
                FindMyPetQuery(adopter_profile_id=world["profile_one"]), session
            )

        shown = next(item for item in ranked if item.animal_id == world["dog"])
        assert shown.analysis is None

    def test_a_matching_direction_analysis_is_attached(
        self, session_factory: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Proves the join works when the direction is the right one."""
        with session_factory() as session:
            session.add(
                _analysis(
                    world["profile_one"], world["dog"], score=42,
                    direction=MatchDirection.ADOPTER_TO_ANIMAL,
                )
            )
            session.commit()

        with session_factory() as session:
            ranked = FindMyPetHandler().handle(
                FindMyPetQuery(adopter_profile_id=world["profile_one"]), session
            )

        shown = next(item for item in ranked if item.animal_id == world["dog"])
        assert shown.analysis is not None


def _analysis(
    profile_id: str,
    animal_id: str,
    score: int,
    direction: MatchDirection = MatchDirection.ANIMAL_TO_ADOPTER,
) -> MatchAnalysis:
    """Build a stored analysis row with the minimum the read models need."""
    return MatchAnalysis(
        match_analysis_id=new_identifier(), direction=direction.value,
        adopter_profile_id=profile_id, animal_id=animal_id, score=score,
        is_disqualified=False, criterion_scores="[]", reasons='["A reason."]',
        concerns="[]", missing_information="[]", evidence_sources="[]",
        used_web_search=False, model_name="stub-model", generated_at=_now(),
    )
