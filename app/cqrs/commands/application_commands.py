"""Commands that change application state.

Each handler validates against the domain rules, appends the events that
record what happened, and updates the projection. All three occur inside the
transaction the bus opened, so a partially-applied cascade can never commit.

Per the CQRS contract, every handler returns an identifier or nothing - never
read data (blueprint section 9.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cqrs.base import Command, CommandHandler
from app.domain.application_rules import (
    ApplicationSnapshot,
    animal_status_after_approval,
    animal_status_after_reversal,
    ensure_application_may_be_submitted,
    ensure_application_transition_allowed,
    select_applications_to_close,
    select_applications_to_reopen,
)
from app.domain.enums import (
    AggregateType,
    AnalysisJobStatus,
    AnalysisJobType,
    AnimalStatus,
    ApplicationStatus,
    DomainEventType,
    NotificationType,
)
from app.eventstore.store import EventStore
from app.infrastructure.models import (
    AdopterProfile,
    AdoptionApplication,
    AnalysisJob,
    Animal,
    Notification,
    new_identifier,
)


def _now() -> datetime:
    """Current UTC time, naive to match the SQL Server DATETIME columns."""
    return datetime.now(UTC).replace(tzinfo=None)


class RecordNotFoundError(LookupError):
    """Raised when a command targets a record that does not exist."""


class NotYourRecordError(PermissionError):
    """Raised when a command targets a record belonging to somebody else."""


# --------------------------------------------------------------------------
# Submit
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SubmitApplicationCommand(Command):
    """An adopter applies to adopt one animal."""

    adopter_profile_id: str
    animal_id: str
    actor_user_id: str
    applicant_message: str | None = None


class SubmitApplicationHandler(CommandHandler[str]):
    """Creates an application and queues its analysis."""

    def handle(self, command: Command, session: Session) -> str:
        """Validate, record and queue.

        Returns:
            The new application's identifier. Deliberately not the
            application itself: a command returns no read data.

        Raises:
            RecordNotFoundError: The adopter or animal does not exist.
            ApplicationNotAllowedError: A business precondition failed.
        """
        assert isinstance(command, SubmitApplicationCommand)

        animal = session.get(Animal, command.animal_id)
        profile = session.get(AdopterProfile, command.adopter_profile_id)
        if animal is None or profile is None:
            raise RecordNotFoundError("Adopter profile or animal does not exist.")

        active_for_this_animal = [
            row.application_id
            for row in _applications_of(session, command.adopter_profile_id)
            if row.animal_id == command.animal_id
            and ApplicationStatus(row.status).is_active
        ]

        ensure_application_may_be_submitted(
            animal_status=AnimalStatus(animal.status),
            adopter_profile_is_complete=bool(profile.is_complete),
            existing_active_application_ids=active_for_this_animal,
        )

        application_id = new_identifier()
        submitted_at = _now()

        session.add(
            AdoptionApplication(
                application_id=application_id,
                adopter_profile_id=command.adopter_profile_id,
                animal_id=command.animal_id,
                status=ApplicationStatus.SUBMITTED.value,
                applicant_message=command.applicant_message,
                submitted_at=submitted_at,
            )
        )

        EventStore(session).append(
            DomainEventType.APPLICATION_SUBMITTED,
            AggregateType.APPLICATION,
            application_id,
            payload={"animal_id": command.animal_id, "animal_name": animal.name},
            actor_user_id=command.actor_user_id,
        )

        # The agent picks this up asynchronously. The adopter never waits on
        # a model (NFR-3.1).
        session.add(
            AnalysisJob(
                analysis_job_id=new_identifier(),
                job_type=AnalysisJobType.RANK_APPLICANT.value,
                status=AnalysisJobStatus.PENDING.value,
                adopter_profile_id=command.adopter_profile_id,
                animal_id=command.animal_id,
                application_id=application_id,
                attempt_count=0,
                created_at=submitted_at,
            )
        )

        return application_id


# --------------------------------------------------------------------------
# Withdraw
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WithdrawApplicationCommand(Command):
    """An adopter withdraws their own application.

    Carries the withdrawing adopter's profile identifier so the handler can
    prove the application is theirs (FR-2.4).
    """

    application_id: str
    actor_user_id: str
    adopter_profile_id: str


class WithdrawApplicationHandler(CommandHandler[None]):
    """Marks an application withdrawn."""

    def handle(self, command: Command, session: Session) -> None:
        """Validate the transition and record the withdrawal.

        Raises:
            RecordNotFoundError: The application does not exist.
            NotYourRecordError: It belongs to a different adopter.
            IllegalTransitionError: It is already in a final state.
        """
        assert isinstance(command, WithdrawApplicationCommand)

        application = session.get(AdoptionApplication, command.application_id)
        if application is None:
            raise RecordNotFoundError("Application does not exist.")

        # Ownership belongs with the rule, not only in the controller.
        if application.adopter_profile_id != command.adopter_profile_id:
            raise NotYourRecordError("This application belongs to another adopter.")

        ensure_application_transition_allowed(
            ApplicationStatus(application.status), ApplicationStatus.WITHDRAWN
        )

        application.status = ApplicationStatus.WITHDRAWN.value
        application.decided_at = _now()

        EventStore(session).append(
            DomainEventType.APPLICATION_WITHDRAWN,
            AggregateType.APPLICATION,
            application.application_id,
            payload={"animal_id": application.animal_id},
            actor_user_id=command.actor_user_id,
        )


# --------------------------------------------------------------------------
# Approve, with the spec 7.5 cascade
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ApproveApplicationCommand(Command):
    """A staff member approves an application.

    The decision is a human one. Nothing in the agent can issue this command
    (rule R4).
    """

    application_id: str
    staff_user_id: str


class ApproveApplicationHandler(CommandHandler[int]):
    """Approves one application and closes the adopter's other active ones."""

    def handle(self, command: Command, session: Session) -> int:
        """Apply the approval cascade described in spec section 7.5.

        Every closure records *which* approval caused it. That identifier is
        what later makes a reversal reopen exactly the right applications
        rather than guessing.

        Returns:
            How many other applications were closed.

        Raises:
            RecordNotFoundError: The application does not exist.
            IllegalTransitionError: It cannot be approved from its status.
        """
        assert isinstance(command, ApproveApplicationCommand)

        application = session.get(AdoptionApplication, command.application_id)
        if application is None:
            raise RecordNotFoundError("Application does not exist.")

        ensure_application_transition_allowed(
            ApplicationStatus(application.status), ApplicationStatus.APPROVED
        )

        event_store = EventStore(session)
        decided_at = _now()

        application.status = ApplicationStatus.APPROVED.value
        application.decided_at = decided_at
        application.decided_by_user_id = command.staff_user_id

        event_store.append(
            DomainEventType.APPLICATION_APPROVED,
            AggregateType.APPLICATION,
            application.application_id,
            payload={"animal_id": application.animal_id},
            actor_user_id=command.staff_user_id,
        )

        closed = self._close_other_active_applications(
            session, event_store, application, command.staff_user_id
        )

        self._move_animal_to(
            session,
            event_store,
            application.animal_id,
            animal_status_after_approval(),
            command.staff_user_id,
        )

        _notify(
            session,
            _user_id_of(session, application.adopter_profile_id),
            NotificationType.APPLICATION_STATUS_CHANGED,
            "Your application was approved",
            "Congratulations. A staff member will be in touch about next steps.",
        )

        return closed

    def _close_other_active_applications(
        self,
        session: Session,
        event_store: EventStore,
        approved: AdoptionApplication,
        staff_user_id: str,
    ) -> int:
        """Close the adopter's other active applications, recording the cause."""
        snapshots = [
            _to_snapshot(row)
            for row in _applications_of(session, approved.adopter_profile_id)
        ]
        to_close = select_applications_to_close(approved.application_id, snapshots)

        for snapshot in to_close:
            row = session.get(AdoptionApplication, snapshot.application_id)
            if row is None:
                continue

            row.status = ApplicationStatus.CLOSED.value
            # The projection of the event below. This column is what makes the
            # reversal query a lookup instead of a guess.
            row.closed_because_application_id = approved.application_id

            event_store.append(
                DomainEventType.APPLICATION_CLOSED_DUE_TO_OTHER_APPROVAL,
                AggregateType.APPLICATION,
                row.application_id,
                payload={
                    "caused_by_application_id": approved.application_id,
                    "animal_id": row.animal_id,
                },
                actor_user_id=staff_user_id,
            )

        return len(to_close)

    @staticmethod
    def _move_animal_to(
        session: Session,
        event_store: EventStore,
        animal_id: str,
        target_status: AnimalStatus,
        actor_user_id: str,
    ) -> None:
        """Change an animal's status and record the change."""
        animal = session.get(Animal, animal_id)
        if animal is None:
            return

        previous_status = animal.status
        animal.status = target_status.value
        animal.updated_at = _now()

        event_store.append(
            DomainEventType.ANIMAL_STATUS_CHANGED,
            AggregateType.ANIMAL,
            animal_id,
            payload={"from": previous_status, "to": target_status.value},
            actor_user_id=actor_user_id,
        )


# --------------------------------------------------------------------------
# Reverse an approval, with the spec 7.5 reopen rule
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReverseApprovalCommand(Command):
    """An approved adoption falls through and is undone."""

    application_id: str
    staff_user_id: str
    reason: str | None = None


class ReverseApprovalHandler(CommandHandler[int]):
    """Undoes an approval and reopens exactly what that approval closed."""

    def handle(self, command: Command, session: Session) -> int:
        """Reverse an approval.

        Reopens only the applications carrying this approval's identifier as
        their closure cause. Applications the adopter withdrew, or that were
        rejected on their own merits, are deliberately left alone - nobody
        reversed those decisions.

        Returns:
            How many applications were reopened.

        Raises:
            RecordNotFoundError: The application does not exist.
            IllegalTransitionError: It was not in an approved state.
        """
        assert isinstance(command, ReverseApprovalCommand)

        approved = session.get(AdoptionApplication, command.application_id)
        if approved is None:
            raise RecordNotFoundError("Application does not exist.")

        ensure_application_transition_allowed(
            ApplicationStatus(approved.status), ApplicationStatus.WITHDRAWN
        )

        event_store = EventStore(session)

        approved.status = ApplicationStatus.WITHDRAWN.value
        approved.decided_at = _now()

        event_store.append(
            DomainEventType.APPLICATION_WITHDRAWN,
            AggregateType.APPLICATION,
            approved.application_id,
            payload={
                "animal_id": approved.animal_id,
                "reason": command.reason or "approval reversed",
                "reverses_approval": True,
            },
            actor_user_id=command.staff_user_id,
        )

        snapshots = [
            _to_snapshot(row)
            for row in _applications_of(session, approved.adopter_profile_id)
        ]
        to_reopen = select_applications_to_reopen(approved.application_id, snapshots)

        for snapshot in to_reopen:
            row = session.get(AdoptionApplication, snapshot.application_id)
            if row is None:
                continue

            row.status = ApplicationStatus.SUBMITTED.value
            row.closed_because_application_id = None

            event_store.append(
                DomainEventType.APPLICATION_REOPENED,
                AggregateType.APPLICATION,
                row.application_id,
                payload={
                    "reopened_because_reversal_of": approved.application_id,
                    "animal_id": row.animal_id,
                },
                actor_user_id=command.staff_user_id,
            )

        ApproveApplicationHandler._move_animal_to(
            session,
            event_store,
            approved.animal_id,
            animal_status_after_reversal(),
            command.staff_user_id,
        )

        return len(to_reopen)


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def _applications_of(session: Session, adopter_profile_id: str) -> list[AdoptionApplication]:
    """Every application belonging to one adopter."""
    return list(
        session.execute(
            select(AdoptionApplication).where(
                AdoptionApplication.adopter_profile_id == adopter_profile_id
            )
        )
        .scalars()
        .all()
    )


def _to_snapshot(row: AdoptionApplication) -> ApplicationSnapshot:
    """Convert a row into the value object the domain rules operate on."""
    return ApplicationSnapshot(
        application_id=row.application_id,
        adopter_profile_id=row.adopter_profile_id,
        animal_id=row.animal_id,
        status=ApplicationStatus(row.status),
        closed_because_application_id=row.closed_because_application_id,
    )


def _user_id_of(session: Session, adopter_profile_id: str) -> str | None:
    """Find the user behind an adopter profile."""
    profile = session.get(AdopterProfile, adopter_profile_id)
    return profile.user_id if profile else None


def _notify(
    session: Session,
    user_id: str | None,
    notification_type: NotificationType,
    title: str,
    body: str,
) -> None:
    """Add an internal inbox message (spec section 23)."""
    if user_id is None:
        return

    session.add(
        Notification(
            notification_id=new_identifier(),
            user_id=user_id,
            notification_type=notification_type.value,
            title=title,
            body=body,
            link_url="/my/applications",
            is_read=False,
            created_at=_now(),
        )
    )
