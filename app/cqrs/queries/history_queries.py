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


@dataclass(frozen=True)
class AggregateHistory:
    """The full recorded history of one aggregate."""

    aggregate_type: str
    aggregate_id: str
    title: str
    entries: list[HistoryEntry]


@dataclass(frozen=True)
class GetAggregateHistoryQuery(Query):
    """Fetch every recorded event for one aggregate."""

    aggregate_type: str
    aggregate_id: str


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
        """
        assert isinstance(query, GetAggregateHistoryQuery)

        try:
            aggregate_type = AggregateType(query.aggregate_type)
        except ValueError:
            return None

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
