"""Rebuilding current state by replaying the event log (blueprint section 10).

The event log is the source of truth; the `adoption_applications` and
`adoption_invitations` tables are projections of it, maintained as events are
appended because a read model that has to replay a log is not a read model.
This module is the other direction: given the events, what *should* those rows
say (FR-13.3)?

That matters for three reasons.

**It proves the log is complete.** If replaying every stream reproduces every
row, then nothing has been written to a row that was not also recorded as an
event - which is the property the whole design rests on and the one nothing
had checked.

**It makes the spec 7.5 rule inspectable.** Reversing an approval must reopen
exactly the applications that approval closed. The causation is on the closing
event; `closed_because_application_id` is a projection of it. A drift between
the two would silently reopen the wrong applications, and `rebuild_projections`
is how that drift is found.

**It is a repair tool.** With `apply=True` the replayed values are written back
over the rows, inside the caller's transaction.

Nothing here writes unless asked to, and nothing here commits: the caller owns
the transaction, so a rebuild either applies completely or not at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy.orm import Session

from app.domain.enums import (
    AggregateType,
    ApplicationStatus,
    DomainEventType,
    InvitationStatus,
)
from app.eventstore.store import EventStore, RecordedEvent
from app.infrastructure.models import AdoptionApplication, AdoptionInvitation

# Which status each event leaves an application in. Lifted from the throwaway
# reducer in tests/integration/test_approval_cascade.py, which proved the
# property and then threw the knowledge away; the test now imports this.
#
# APPLICATION_REOPENED maps to SUBMITTED rather than to a status of its own:
# reopening returns an application to the queue, and spec section 25 has no
# "reopened" state for it to sit in.
STATUS_AFTER_APPLICATION_EVENT: dict[DomainEventType, ApplicationStatus] = {
    DomainEventType.APPLICATION_SUBMITTED: ApplicationStatus.SUBMITTED,
    DomainEventType.APPLICATION_UNDER_REVIEW: ApplicationStatus.UNDER_REVIEW,
    DomainEventType.APPLICATION_APPROVED: ApplicationStatus.APPROVED,
    DomainEventType.APPLICATION_REJECTED: ApplicationStatus.REJECTED,
    DomainEventType.APPLICATION_WITHDRAWN: ApplicationStatus.WITHDRAWN,
    DomainEventType.APPLICATION_CLOSED_DUE_TO_OTHER_APPROVAL: ApplicationStatus.CLOSED,
    DomainEventType.APPLICATION_REOPENED: ApplicationStatus.SUBMITTED,
}

STATUS_AFTER_INVITATION_EVENT: dict[DomainEventType, InvitationStatus] = {
    DomainEventType.INVITATION_SENT: InvitationStatus.SENT,
    DomainEventType.INVITATION_VIEWED: InvitationStatus.VIEWED,
    DomainEventType.INVITATION_ACCEPTED: InvitationStatus.ACCEPTED,
    DomainEventType.INVITATION_DECLINED: InvitationStatus.DECLINED,
    DomainEventType.INVITATION_EXPIRED: InvitationStatus.EXPIRED,
}


class EmptyStreamError(ValueError):
    """Raised when a replay is asked for an aggregate with no events.

    Distinct from "the aggregate is in its initial state": an aggregate whose
    stream is empty was never created, so there is nothing to rebuild and a
    default-valued projection would be a fabrication.
    """


@dataclass(frozen=True)
class ApplicationProjection:
    """The current state of one application, derived from its events."""

    application_id: str
    status: ApplicationStatus
    animal_id: str | None
    closed_because_application_id: str | None
    decided_by_user_id: str | None
    submitted_at: datetime | None
    decided_at: datetime | None


@dataclass(frozen=True)
class InvitationProjection:
    """The current state of one invitation, derived from its events."""

    invitation_id: str
    status: InvitationStatus
    animal_id: str | None
    adopter_profile_id: str | None
    sent_at: datetime | None
    viewed_at: datetime | None
    responded_at: datetime | None


@dataclass(frozen=True)
class ProjectionMismatch:
    """One field where the stored row and the replayed state disagree."""

    aggregate_type: str
    aggregate_id: str
    field_name: str
    replayed: str
    stored: str

    def __str__(self) -> str:
        """Describe the disagreement in one line, for a report or a failure."""
        return (
            f"{self.aggregate_type} {self.aggregate_id}: {self.field_name} "
            f"replayed as {self.replayed!r} but is stored as {self.stored!r}"
        )


@dataclass(frozen=True)
class RebuildReport:
    """What a rebuild found, and whether it changed anything."""

    matching: int = 0
    mismatching: int = 0
    missing: int = 0
    mismatches: tuple[ProjectionMismatch, ...] = ()
    applied: bool = False

    @property
    def total_streams(self) -> int:
        """How many aggregates were replayed."""
        return self.matching + self.mismatching + self.missing

    @property
    def is_consistent(self) -> bool:
        """Whether every replayed aggregate matches a stored row exactly."""
        return self.mismatching == 0 and self.missing == 0


def replay_application(events: Sequence[RecordedEvent]) -> ApplicationProjection:
    """Rebuild one application's current state from its own events.

    Events are applied in the order given, which is the order
    `EventStore.read_aggregate_stream` returns them: by sequence number, not
    by clock. Two events appended in the same second must still replay in the
    order they happened.

    Args:
        events: One application's stream, oldest first.

    Returns:
        The state those events leave the application in.

    Raises:
        EmptyStreamError: The stream is empty, so there is no application.
    """
    first = _first_event(events)
    state = ApplicationProjection(
        application_id=first.aggregate_id,
        status=ApplicationStatus.SUBMITTED,
        animal_id=None,
        closed_because_application_id=None,
        decided_by_user_id=None,
        submitted_at=None,
        decided_at=None,
    )

    for event in events:
        state = _apply_application_event(state, event)
    return state


def replay_invitation(events: Sequence[RecordedEvent]) -> InvitationProjection:
    """Rebuild one invitation's current state from its own events.

    Args:
        events: One invitation's stream, oldest first.

    Returns:
        The state those events leave the invitation in.

    Raises:
        EmptyStreamError: The stream is empty, so there is no invitation.
    """
    first = _first_event(events)
    state = InvitationProjection(
        invitation_id=first.aggregate_id,
        status=InvitationStatus.SENT,
        animal_id=None,
        adopter_profile_id=None,
        sent_at=None,
        viewed_at=None,
        responded_at=None,
    )

    for event in events:
        state = _apply_invitation_event(state, event)
    return state


def rebuild_projections(session: Session, apply: bool = False) -> RebuildReport:
    """Replay every application and invitation and compare to the live rows.

    Read-only by default (FR-13.3): it answers "does the log still explain
    the database?" without touching anything, which is what makes it safe to
    run against the production database and useful as a test.

    Args:
        session: An open session. With `apply` the caller's transaction is
            written to but never committed here, so a rebuild applies
            completely or not at all.
        apply: Write the replayed values back over the stored rows. The
            repair mode: use it when a comparison has already shown what
            would change.

    Returns:
        Counts of matching, mismatching and missing aggregates, with one
        entry per disagreeing field.
    """
    streams = _streams_by_aggregate(session)
    mismatches: list[ProjectionMismatch] = []
    matching = 0
    missing = 0

    for (aggregate_type, aggregate_id), events in streams.items():
        found = _compare_one(session, aggregate_type, aggregate_id, events, apply)
        if found is None:
            missing += 1
        elif found:
            mismatches.extend(found)
        else:
            matching += 1

    return RebuildReport(
        matching=matching,
        mismatching=len({mismatch.aggregate_id for mismatch in mismatches}),
        missing=missing,
        mismatches=tuple(mismatches),
        applied=apply,
    )


# --------------------------------------------------------------------------
# Replaying one event
# --------------------------------------------------------------------------


def _first_event(events: Sequence[RecordedEvent]) -> RecordedEvent:
    """Return the first event of a stream, refusing an empty one.

    Raises:
        EmptyStreamError: There are no events to rebuild from.
    """
    if not events:
        raise EmptyStreamError("Cannot replay an aggregate with no events.")
    return events[0]


def _apply_application_event(
    state: ApplicationProjection, event: RecordedEvent
) -> ApplicationProjection:
    """Fold one event into an application's state.

    Every field is derived from what the event records, never from the row
    the event is about - that is the whole point of a replay.

    Args:
        state: The state so far.
        event: The next event in the stream.

    Returns:
        The state after that event. Unrecognised event types - an analysis
        completing against this application, for instance - leave the state
        untouched rather than being an error: not every event about an
        aggregate changes its status.
    """
    status = STATUS_AFTER_APPLICATION_EVENT.get(event.event_type)
    if status is None:
        return state

    moved = replace(
        state,
        status=status,
        animal_id=_stored_text(event.payload.get("animal_id")) or state.animal_id,
    )

    if event.event_type is DomainEventType.APPLICATION_SUBMITTED:
        return replace(moved, submitted_at=event.occurred_at)

    if event.event_type is DomainEventType.APPLICATION_CLOSED_DUE_TO_OTHER_APPROVAL:
        return replace(
            moved,
            closed_because_application_id=_stored_text(
                event.payload.get("caused_by_application_id")
            ),
        )

    if event.event_type is DomainEventType.APPLICATION_REOPENED:
        # The closure is undone, so its cause stops applying. Leaving it set
        # is what would make a second reversal reopen this twice.
        return replace(moved, closed_because_application_id=None)

    if status.is_final:
        return replace(
            moved, decided_at=event.occurred_at, decided_by_user_id=event.actor_user_id
        )

    return moved


def _apply_invitation_event(
    state: InvitationProjection, event: RecordedEvent
) -> InvitationProjection:
    """Fold one event into an invitation's state.

    Args:
        state: The state so far.
        event: The next event in the stream.

    Returns:
        The state after that event, unchanged for an event that does not
        move an invitation.
    """
    status = STATUS_AFTER_INVITATION_EVENT.get(event.event_type)
    if status is None:
        return state

    moved = replace(
        state,
        status=status,
        animal_id=_stored_text(event.payload.get("animal_id")) or state.animal_id,
        adopter_profile_id=(
            _stored_text(event.payload.get("adopter_profile_id"))
            or state.adopter_profile_id
        ),
    )

    if event.event_type is DomainEventType.INVITATION_SENT:
        return replace(moved, sent_at=event.occurred_at)

    if event.event_type is DomainEventType.INVITATION_VIEWED:
        return replace(moved, viewed_at=event.occurred_at)

    if not status.is_awaiting_response:
        return replace(moved, responded_at=event.occurred_at)

    return moved


def _stored_text(raw_value: object) -> str | None:
    """Read one string out of a decoded payload, or None if it is not one.

    Payloads are JSON that another process may have written, so a field can
    be absent or the wrong shape. A value that is not a string is treated as
    absent rather than coerced, because `str(None)` would quietly become the
    identifier `"None"`.
    """
    return raw_value if isinstance(raw_value, str) else None


# --------------------------------------------------------------------------
# Comparing a replay to the stored row
# --------------------------------------------------------------------------


# The aggregates this module rebuilds. Animals and profiles are deliberately
# out of scope: their events record status changes and edits rather than a
# complete history of every column, so replaying one would not reconstruct
# the row and pretending otherwise would be worse than not offering it.
REBUILDABLE_AGGREGATES = (AggregateType.APPLICATION, AggregateType.INVITATION)


def _streams_by_aggregate(
    session: Session,
) -> dict[tuple[AggregateType, str], list[RecordedEvent]]:
    """Group the whole log into one stream per rebuildable aggregate.

    One pass over the log rather than a query per aggregate: this runs
    against a shared free-tier database, where several hundred round trips
    would be the slowest thing in the test suite. The two kinds this module
    can rebuild are selected in SQL rather than filtered here, so the animal
    and profile events - which outnumber them - are never fetched at all.

    Args:
        session: A session to read the log through.

    Returns:
        Events per aggregate, each list in sequence order.
    """
    streams: dict[tuple[AggregateType, str], list[RecordedEvent]] = {}
    for event in EventStore(session).read_all(aggregate_types=REBUILDABLE_AGGREGATES):
        streams.setdefault((event.aggregate_type, event.aggregate_id), []).append(event)

    for stream in streams.values():
        stream.sort(key=lambda event: event.sequence_number)
    return streams


def _compare_one(
    session: Session,
    aggregate_type: AggregateType,
    aggregate_id: str,
    events: list[RecordedEvent],
    apply: bool,
) -> list[ProjectionMismatch] | None:
    """Compare one replayed aggregate with its stored row.

    Args:
        session: An open session.
        aggregate_type: Which kind of aggregate this is.
        aggregate_id: Its identifier.
        events: Its stream, in sequence order.
        apply: Whether to write the replayed values back.

    Returns:
        The disagreeing fields (an empty list when they agree), or None when
        the log describes an aggregate that has no row at all.
    """
    if aggregate_type is AggregateType.APPLICATION:
        row = session.get(AdoptionApplication, aggregate_id)
        if row is None:
            return None
        return _compare_application(row, replay_application(events), apply)

    invitation = session.get(AdoptionInvitation, aggregate_id)
    if invitation is None:
        return None
    return _compare_invitation(invitation, replay_invitation(events), apply)


def _compare_application(
    row: AdoptionApplication, replayed: ApplicationProjection, apply: bool
) -> list[ProjectionMismatch]:
    """Compare the two fields the spec 7.5 rule depends on.

    Status and closure cause, and not the timestamps: `decided_at` is a clock
    reading taken when the command ran, while the event's `occurred_at` is
    taken a moment later inside the store, so they differ by microseconds
    for reasons that mean nothing.

    Args:
        row: The stored projection.
        replayed: What the log says it should be.
        apply: Whether to write the replayed values over the row.

    Returns:
        One entry per disagreeing field.
    """
    found = _differences(
        AggregateType.APPLICATION.value,
        row.application_id,
        {
            "status": (replayed.status.value, row.status),
            "closed_because_application_id": (
                replayed.closed_because_application_id,
                row.closed_because_application_id,
            ),
        },
    )

    if apply and found:
        row.status = replayed.status.value
        row.closed_because_application_id = replayed.closed_because_application_id

    return found


def _compare_invitation(
    row: AdoptionInvitation, replayed: InvitationProjection, apply: bool
) -> list[ProjectionMismatch]:
    """Compare a stored invitation with its replayed state.

    Args:
        row: The stored projection.
        replayed: What the log says it should be.
        apply: Whether to write the replayed status over the row.

    Returns:
        One entry per disagreeing field.
    """
    found = _differences(
        AggregateType.INVITATION.value,
        row.invitation_id,
        {"status": (replayed.status.value, row.status)},
    )

    if apply and found:
        row.status = replayed.status.value

    return found


def _differences(
    aggregate_type: str,
    aggregate_id: str,
    fields: dict[str, tuple[str | None, str | None]],
) -> list[ProjectionMismatch]:
    """Report the fields whose replayed and stored values differ.

    Args:
        aggregate_type: For the report line.
        aggregate_id: For the report line.
        fields: Field name mapped to (replayed, stored).

    Returns:
        One mismatch per field that disagrees.
    """
    return [
        ProjectionMismatch(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            field_name=field_name,
            replayed=str(replayed),
            stored=str(stored),
        )
        for field_name, (replayed, stored) in fields.items()
        if replayed != stored
    ]
