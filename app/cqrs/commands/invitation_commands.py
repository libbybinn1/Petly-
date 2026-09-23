"""Commands for the invitation lifecycle (spec sections 7.4 and 25).

Sending, responding to, and expiring invitations. Each handler validates
against the domain rules, appends its event and updates the projection inside
the transaction the bus opened.

The rule worth remembering: **accepting an invitation creates an application,
it does not approve an adoption.** The human decision still happens later.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cqrs.base import Command, CommandHandler
from app.cqrs.commands.application_commands import NotYourRecordError, RecordNotFoundError
from app.domain.enums import (
    AggregateType,
    AnalysisJobStatus,
    AnalysisJobType,
    AnimalStatus,
    ApplicationStatus,
    DomainEventType,
    InvitationStatus,
    NotificationType,
)
from app.domain.invitation_rules import (
    InvitationEligibility,
    calculate_expiry,
    ensure_invitation_may_be_sent,
    ensure_response_is_allowed,
    status_after_expiry_sweep,
)
from app.eventstore.store import EventStore
from app.infrastructure.models import (
    AdopterProfile,
    AdoptionApplication,
    AdoptionInvitation,
    AnalysisJob,
    Animal,
    Notification,
    User,
    new_identifier,
)


def _utc_now() -> datetime:
    """Timezone-aware current time, for the 72-hour arithmetic."""
    return datetime.now(UTC)


def _naive(moment: datetime) -> datetime:
    """Strip the timezone for storage.

    SQL Server 2014 DATETIME columns are naive. Everything is stored as UTC
    and read back as UTC; the conversion happens only at the boundary.
    """
    return moment.replace(tzinfo=None)


def _aware(moment: datetime) -> datetime:
    """Re-attach UTC to a value read from the database."""
    return moment.replace(tzinfo=UTC)


# --------------------------------------------------------------------------
# Send
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SendInvitationCommand(Command):
    """A staff member invites an adopter to consider one animal."""

    animal_id: str
    adopter_profile_id: str
    staff_user_id: str
    staff_message: str | None = None


class SendInvitationHandler(CommandHandler[str]):
    """Creates an invitation with a 72-hour response window."""

    def handle(self, command: Command, session: Session) -> str:
        """Validate eligibility, record the invitation and notify the adopter.

        Returns:
            The new invitation's identifier.

        Raises:
            RecordNotFoundError: The animal or adopter does not exist.
            InvitationNotAllowedError: An eligibility rule failed.
        """
        assert isinstance(command, SendInvitationCommand)

        animal = session.get(Animal, command.animal_id)
        profile = session.get(AdopterProfile, command.adopter_profile_id)
        if animal is None or profile is None:
            raise RecordNotFoundError("Animal or adopter profile does not exist.")

        user = session.get(User, profile.user_id)

        ensure_invitation_may_be_sent(
            InvitationEligibility(
                adopter_profile_is_complete=bool(profile.is_complete),
                adopter_opted_in=bool(profile.open_to_proactive_suggestions),
                adopter_account_is_active=bool(user and user.is_active),
                animal_status=AnimalStatus(animal.status),
                has_open_invitation_for_this_animal=_has_open_invitation(
                    session, command.adopter_profile_id, command.animal_id
                ),
                has_active_application_for_this_animal=_has_active_application(
                    session, command.adopter_profile_id, command.animal_id
                ),
            )
        )

        sent_at = _utc_now()
        expires_at = calculate_expiry(sent_at)
        invitation_id = new_identifier()

        session.add(
            AdoptionInvitation(
                invitation_id=invitation_id,
                animal_id=command.animal_id,
                adopter_profile_id=command.adopter_profile_id,
                sent_by_user_id=command.staff_user_id,
                status=InvitationStatus.SENT.value,
                staff_message=command.staff_message,
                sent_at=_naive(sent_at),
                expires_at=_naive(expires_at),
            )
        )

        EventStore(session).append(
            DomainEventType.INVITATION_SENT,
            AggregateType.INVITATION,
            invitation_id,
            payload={
                "animal_id": command.animal_id,
                "animal_name": animal.name,
                "adopter_profile_id": command.adopter_profile_id,
                "expires_at": expires_at.isoformat(),
            },
            actor_user_id=command.staff_user_id,
        )

        _notify(
            session,
            profile.user_id,
            NotificationType.INVITATION_RECEIVED,
            f"Invitation to meet {animal.name}",
            f"A staff member thinks {animal.name} could suit your home. "
            f"You have 72 hours to respond.",
            "/my/invitations",
        )

        return invitation_id


# --------------------------------------------------------------------------
# View
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MarkInvitationViewedCommand(Command):
    """The adopter opened their invitation."""

    invitation_id: str
    actor_user_id: str
    adopter_profile_id: str


class MarkInvitationViewedHandler(CommandHandler[None]):
    """Records that an invitation was seen."""

    def handle(self, command: Command, session: Session) -> None:
        """Move SENT to VIEWED, ignoring anything already further along.

        Idempotent on purpose: opening the page twice must not append two
        events or overwrite a recorded response.
        """
        assert isinstance(command, MarkInvitationViewedCommand)

        invitation = session.get(AdoptionInvitation, command.invitation_id)
        if invitation is None:
            raise RecordNotFoundError("Invitation does not exist.")

        if invitation.adopter_profile_id != command.adopter_profile_id:
            raise NotYourRecordError("This invitation belongs to another adopter.")

        if InvitationStatus(invitation.status) is not InvitationStatus.SENT:
            return

        invitation.status = InvitationStatus.VIEWED.value
        invitation.viewed_at = _naive(_utc_now())

        EventStore(session).append(
            DomainEventType.INVITATION_VIEWED,
            AggregateType.INVITATION,
            invitation.invitation_id,
            payload={"animal_id": invitation.animal_id},
            actor_user_id=command.actor_user_id,
        )


# --------------------------------------------------------------------------
# Respond
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RespondToInvitationCommand(Command):
    """The adopter accepts or declines.

    Carries the responder's own profile identifier so the handler can prove
    the invitation is theirs. Without it the command would act on any
    identifier it was handed (FR-2.4).
    """

    invitation_id: str
    actor_user_id: str
    adopter_profile_id: str
    accepted: bool


class RespondToInvitationHandler(CommandHandler[str | None]):
    """Records a response, creating an application when accepted."""

    def handle(self, command: Command, session: Session) -> str | None:
        """Accept or decline an invitation.

        Accepting **creates an application**; it does not approve an adoption
        (spec section 7.4). The staff decision comes later, through the
        ordinary approval command.

        Returns:
            The new application's identifier when accepted, otherwise None.

        Raises:
            RecordNotFoundError: The invitation does not exist.
            NotYourRecordError: It belongs to a different adopter.
            InvitationExpiredError: The 72-hour window has closed.
            InvitationNotAllowedError: It was already answered.
        """
        assert isinstance(command, RespondToInvitationCommand)

        invitation = session.get(AdoptionInvitation, command.invitation_id)
        if invitation is None:
            raise RecordNotFoundError("Invitation does not exist.")

        # Ownership is checked here, in the command, rather than only in the
        # controller. A route is one caller; the rule belongs with the rule.
        if invitation.adopter_profile_id != command.adopter_profile_id:
            raise NotYourRecordError("This invitation belongs to another adopter.")

        now = _utc_now()
        ensure_response_is_allowed(
            InvitationStatus(invitation.status), _aware(invitation.expires_at), now
        )

        event_store = EventStore(session)
        invitation.responded_at = _naive(now)
        invitation.status = (
            InvitationStatus.ACCEPTED.value
            if command.accepted
            else InvitationStatus.DECLINED.value
        )

        event_store.append(
            DomainEventType.INVITATION_ACCEPTED
            if command.accepted
            else DomainEventType.INVITATION_DECLINED,
            AggregateType.INVITATION,
            invitation.invitation_id,
            payload={"animal_id": invitation.animal_id},
            actor_user_id=command.actor_user_id,
        )

        _notify_sender(session, invitation, command.accepted)

        if not command.accepted:
            return None

        return self._start_application(session, event_store, invitation, command)

    @staticmethod
    def _start_application(
        session: Session,
        event_store: EventStore,
        invitation: AdoptionInvitation,
        command: RespondToInvitationCommand,
    ) -> str:
        """Create the application an accepted invitation leads into."""
        application_id = new_identifier()
        submitted_at = _naive(_utc_now())

        session.add(
            AdoptionApplication(
                application_id=application_id,
                adopter_profile_id=invitation.adopter_profile_id,
                animal_id=invitation.animal_id,
                status=ApplicationStatus.SUBMITTED.value,
                applicant_message="Accepted an invitation from the organization.",
                originating_invitation_id=invitation.invitation_id,
                submitted_at=submitted_at,
            )
        )

        event_store.append(
            DomainEventType.APPLICATION_SUBMITTED,
            AggregateType.APPLICATION,
            application_id,
            payload={
                "animal_id": invitation.animal_id,
                "from_invitation_id": invitation.invitation_id,
            },
            actor_user_id=command.actor_user_id,
        )

        session.add(
            AnalysisJob(
                analysis_job_id=new_identifier(),
                job_type=AnalysisJobType.RANK_APPLICANT.value,
                status=AnalysisJobStatus.PENDING.value,
                adopter_profile_id=invitation.adopter_profile_id,
                animal_id=invitation.animal_id,
                application_id=application_id,
                attempt_count=0,
                created_at=submitted_at,
            )
        )

        return application_id


# --------------------------------------------------------------------------
# Expiry sweep
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ExpireOverdueInvitationsCommand(Command):
    """Close every invitation whose 72-hour window has passed."""


class ExpireOverdueInvitationsHandler(CommandHandler[int]):
    """Marks unanswered, overdue invitations as expired.

    Expiry is applied by an explicit sweep rather than inferred at read time,
    so the transition produces a real `InvitationExpired` event. Without it
    the history would show an invitation that simply stopped mattering, with
    no record of when.
    """

    def handle(self, command: Command, session: Session) -> int:
        """Expire every overdue invitation.

        Returns:
            How many were expired.
        """
        assert isinstance(command, ExpireOverdueInvitationsCommand)

        now = _utc_now()
        awaiting = [status.value for status in InvitationStatus if status.is_awaiting_response]

        candidates = (
            session.execute(
                select(AdoptionInvitation).where(AdoptionInvitation.status.in_(awaiting))
            )
            .scalars()
            .all()
        )

        event_store = EventStore(session)
        expired_count = 0

        for invitation in candidates:
            new_status = status_after_expiry_sweep(
                InvitationStatus(invitation.status), _aware(invitation.expires_at), now
            )
            if new_status is None:
                continue

            invitation.status = new_status.value
            event_store.append(
                DomainEventType.INVITATION_EXPIRED,
                AggregateType.INVITATION,
                invitation.invitation_id,
                payload={"animal_id": invitation.animal_id},
                # No actor: time caused this, not a person.
                actor_user_id=None,
            )
            expired_count += 1

        return expired_count


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _has_open_invitation(
    session: Session, adopter_profile_id: str, animal_id: str
) -> bool:
    """Whether this adopter already has an unanswered invitation for this animal."""
    awaiting = [status.value for status in InvitationStatus if status.is_awaiting_response]
    existing = session.execute(
        select(AdoptionInvitation)
        .where(AdoptionInvitation.adopter_profile_id == adopter_profile_id)
        .where(AdoptionInvitation.animal_id == animal_id)
        .where(AdoptionInvitation.status.in_(awaiting))
    ).first()
    return existing is not None


def _has_active_application(
    session: Session, adopter_profile_id: str, animal_id: str
) -> bool:
    """Whether this adopter already has a live application for this animal."""
    active = [status.value for status in ApplicationStatus if status.is_active]
    existing = session.execute(
        select(AdoptionApplication)
        .where(AdoptionApplication.adopter_profile_id == adopter_profile_id)
        .where(AdoptionApplication.animal_id == animal_id)
        .where(AdoptionApplication.status.in_(active))
    ).first()
    return existing is not None


def _notify_sender(
    session: Session, invitation: AdoptionInvitation, accepted: bool
) -> None:
    """Tell the staff member who sent the invitation how it was answered."""
    animal = session.get(Animal, invitation.animal_id)
    animal_name = animal.name if animal else "an animal"
    verdict = "accepted" if accepted else "declined"

    _notify(
        session,
        invitation.sent_by_user_id,
        NotificationType.INVITATION_RESPONSE,
        f"Invitation {verdict}",
        f"Your invitation for {animal_name} was {verdict}."
        + (" An application has been created." if accepted else ""),
        f"/animals/{invitation.animal_id}/adopters",
    )


def _notify(
    session: Session,
    user_id: str | None,
    notification_type: NotificationType,
    title: str,
    body: str,
    link_url: str,
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
            link_url=link_url,
            is_read=False,
            created_at=_naive(_utc_now()),
        )
    )
