"""Read-side query for one aggregate's event history (blueprint section 10).

Blueprint section 10 requires the system to restore and display the relevant
action history. This reads the event log directly: it *is* the history, so
rendering anything else would be rendering a copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cqrs.base import Query, QueryHandler
from app.cqrs.queries.dashboard_queries import EVENT_DESCRIPTIONS
from app.cqrs.queries.formatting import DisplayDate, to_display_moment
from app.domain.enums import AggregateType, DomainEventType
from app.eventstore.store import EventStore, RecordedEvent
from app.infrastructure.models import (
    AdopterProfile,
    AdoptionApplication,
    AdoptionInvitation,
    Animal,
    User,
)

_MODEL_FOR_AGGREGATE: dict[AggregateType, type[Any]] = {
    AggregateType.ANIMAL: Animal,
    AggregateType.APPLICATION: AdoptionApplication,
    AggregateType.INVITATION: AdoptionInvitation,
    AggregateType.ADOPTER_PROFILE: AdopterProfile,
}


@dataclass(frozen=True)
class HistoryEntry:
    """One event, formatted for a timeline."""

    sequence_number: int
    event_type: str
    event_label: str
    description: str
    occurred_at: datetime
    actor_name: str
    detail: str

    @property
    def was_caused_by_the_system(self) -> bool:
        """Whether time or the agent caused this, rather than a person.

        Worth distinguishing on screen: an invitation that expired and one a
        person declined are very different facts.
        """
        return self.actor_name == "The system"

    @property
    def occurred(self) -> DisplayDate:
        """When this happened, in the three forms a timeline needs."""
        return to_display_moment(self.occurred_at)


@dataclass(frozen=True)
class AggregateHistory:
    """The full recorded history of one aggregate."""

    aggregate_type: str
    aggregate_id: str
    title: str
    entries: list[HistoryEntry]


class HistoryNotVisibleError(PermissionError):
    """Raised when a viewer asks for the history of somebody else's record."""


@dataclass(frozen=True)
class GetAggregateHistoryQuery(Query):
    """Fetch every recorded event for one aggregate.

    Carries the viewer as well as the record, because spec section 7.5 is
    written from the adopter's point of view: an adopter whose application
    was closed by somebody else's approval, and later reopened, should be
    able to see that happen. The rule travels with the data rather than
    living in the controller (rule R2).

    Attributes:
        aggregate_type: One of `AggregateType`'s values.
        aggregate_id: The record to show the history of.
        viewer_adopter_profile_id: The signed-in adopter's profile, if any.
        viewer_is_staff: Whether the viewer may read any record's history.
    """

    aggregate_type: str
    aggregate_id: str
    viewer_adopter_profile_id: str | None = None
    viewer_is_staff: bool = False


class GetAggregateHistoryHandler(QueryHandler[AggregateHistory | None]):
    """Answers GetAggregateHistoryQuery."""

    def handle(self, query: Query, session: Session) -> AggregateHistory | None:
        """Return one aggregate's history, or None if it does not exist.

        "No events yet" and "no such record" are different answers and used
        to give the same 404. An animal only gains an event of its own when
        its status changes, so every animal that had not yet been approved
        for adoption - almost all of them - answered 404 to a History link
        that the staff table rendered anyway.

        Returns:
            The history, empty entries included, or None when the aggregate
            itself cannot be found.

        Raises:
            HistoryNotVisibleError: The record is not the viewer's and they
                are not staff.
        """
        assert isinstance(query, GetAggregateHistoryQuery)

        try:
            aggregate_type = AggregateType(query.aggregate_type)
        except ValueError:
            return None

        _ensure_history_is_visible(session, aggregate_type, query)

        events = _events_about(session, aggregate_type, query.aggregate_id)
        if not events and not _aggregate_exists(session, aggregate_type, query.aggregate_id):
            return None

        actor_names = _resolve_actors(session, events)

        return AggregateHistory(
            aggregate_type=aggregate_type.value,
            aggregate_id=query.aggregate_id,
            title=_title_for(session, aggregate_type, query.aggregate_id, events),
            entries=[
                HistoryEntry(
                    sequence_number=event.sequence_number,
                    event_type=event.event_type.value,
                    event_label=_humanise_event_type(event.event_type),
                    description=EVENT_DESCRIPTIONS.get(
                        event.event_type, event.event_type.value
                    ),
                    occurred_at=event.occurred_at,
                    actor_name=actor_names.get(
                        event.actor_user_id or "", "The system"
                    ),
                    detail=_describe_payload(event.payload),
                )
                for event in events
            ],
        )


# Which aggregates an adopter may see the history of, and the column that
# says whose they are. An animal's history names every applicant, and a
# profile is somebody's household, so neither is on this list: those stay
# staff-only (docs/UX.md section 4).
_OWNED_BY_ADOPTER: dict[AggregateType, type[Any]] = {
    AggregateType.APPLICATION: AdoptionApplication,
    AggregateType.INVITATION: AdoptionInvitation,
}


def _ensure_history_is_visible(
    session: Session, aggregate_type: AggregateType, query: GetAggregateHistoryQuery
) -> None:
    """Raise unless this viewer may read this record's history (FR-2.4).

    A record that does not exist is refused rather than reported absent,
    for a non-staff viewer: answering 404 for "no such application" and 403
    for "not yours" would turn this route into a way of discovering which
    identifiers are real.

    Args:
        session: A read-only session.
        aggregate_type: The kind of record being asked for.
        query: The request, carrying the viewer.

    Raises:
        HistoryNotVisibleError: The viewer is not staff and the record is
            not theirs.
    """
    if query.viewer_is_staff:
        return

    model = _OWNED_BY_ADOPTER.get(aggregate_type)
    if model is not None and query.viewer_adopter_profile_id is not None:
        row = session.get(model, query.aggregate_id)
        if row is not None and row.adopter_profile_id == query.viewer_adopter_profile_id:
            return

    raise HistoryNotVisibleError("This history belongs to somebody else.")


def _events_about(
    session: Session, aggregate_type: AggregateType, aggregate_id: str
) -> list[RecordedEvent]:
    """Collect the events that tell this aggregate's story, oldest first.

    For an application or an invitation that is exactly its own stream. An
    animal is different: its own stream holds only status changes, while
    the events a person means by "this animal's history" - who applied, who
    was invited, which application was approved - belong to the application
    and invitation aggregates and name the animal in their payload. Showing
    only the animal's own stream answered a question nobody asked.

    Args:
        session: A read-only session.
        aggregate_type: The kind of aggregate being viewed.
        aggregate_id: Its identifier.

    Returns:
        The relevant events, ordered oldest first.
    """
    own_stream = EventStore(session).read_aggregate_stream(aggregate_id)
    if aggregate_type is not AggregateType.ANIMAL:
        return own_stream

    referencing = [
        event
        for event in EventStore(session).read_all()
        if event.aggregate_id != aggregate_id
        and event.payload.get("animal_id") == aggregate_id
    ]
    combined = own_stream + referencing
    return sorted(combined, key=lambda event: event.sequence_number)


def _aggregate_exists(
    session: Session, aggregate_type: AggregateType, aggregate_id: str
) -> bool:
    """Whether the record behind this history actually exists.

    Distinguishes an empty history from a wrong identifier, so a mistyped
    URL still answers 404 rather than rendering a convincing blank page.
    """
    model = _MODEL_FOR_AGGREGATE.get(aggregate_type)
    if model is None:
        return False
    return session.get(model, aggregate_id) is not None


def _humanise_event_type(event_type: DomainEventType) -> str:
    """Turn APPLICATION_SUBMITTED into "Application submitted"."""
    return event_type.value.replace("_", " ").capitalize()


def _resolve_actors(
    session: Session, events: list[RecordedEvent]
) -> dict[str, str]:
    """Look up every actor in one query."""
    actor_ids = {event.actor_user_id for event in events if event.actor_user_id}
    if not actor_ids:
        return {}

    rows = session.execute(
        select(User.user_id, User.full_name).where(User.user_id.in_(actor_ids))
    ).tuples().all()
    return dict(rows)


def _title_for(
    session: Session,
    aggregate_type: AggregateType,
    aggregate_id: str,
    events: list[RecordedEvent],
) -> str:
    """Build a readable heading for the timeline."""
    if aggregate_type is AggregateType.ANIMAL:
        animal = session.get(Animal, aggregate_id)
        return f"{animal.name}" if animal else "Animal"

    animal_id = next(
        (event.payload.get("animal_id") for event in events
         if event.payload.get("animal_id")),
        None,
    )
    if animal_id:
        animal = session.get(Animal, animal_id)
        if animal:
            label = aggregate_type.value.lower()
            return f"{label.capitalize()} for {animal.name}"

    return aggregate_type.value


def _describe_payload(payload: dict[str, Any]) -> str:
    """Summarise the parts of a payload worth showing.

    Only the fields that explain *why* something happened are surfaced. The
    rest is noise on a timeline.
    """
    parts: list[str] = []
    if payload.get("caused_by_application_id"):
        parts.append("because another application was approved")
    if payload.get("reopened_because_reversal_of"):
        parts.append("after that approval was reversed")
    if payload.get("reverses_approval"):
        parts.append("reversing an approval")
    if payload.get("from_invitation_id"):
        parts.append("from an invitation")
    if payload.get("score") is not None:
        parts.append(f"score {payload['score']}")
    if payload.get("from") and payload.get("to"):
        parts.append(f"{payload['from']} to {payload['to']}")
    return ", ".join(parts)
