"""Append-only event store (architecture section 4).

The store exposes `append` and read operations only. There is deliberately no
update or delete method, so the immutability of the log is a property of the
API rather than a convention people must remember.

Why event sourcing is used here is documented in docs/ARCHITECTURE.md section
4: spec 7.5 requires reopening exactly those applications that were closed
because a specific approval happened. A current-state-only schema cannot
distinguish those from applications closed for other reasons.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.enums import AggregateType, DomainEventType
from app.infrastructure.models import DomainEvent, new_identifier


@dataclass(frozen=True)
class RecordedEvent:
    """An event read back out of the store, with its payload decoded."""

    event_id: str
    event_type: DomainEventType
    aggregate_type: AggregateType
    aggregate_id: str
    sequence_number: int
    occurred_at: datetime
    actor_user_id: str | None
    payload: dict[str, Any] = field(default_factory=dict)


class EventStore:
    """Append-only access to the domain event log."""

    def __init__(self, session: Session) -> None:
        """Bind the store to a session.

        Args:
            session: An open session. The caller owns the transaction, so a
                command can append several events atomically.
        """
        self._session = session

    def append(
        self,
        event_type: DomainEventType,
        aggregate_type: AggregateType,
        aggregate_id: str,
        payload: dict[str, Any] | None = None,
        actor_user_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> RecordedEvent:
        """Append one event to the log.

        The sequence number is derived from the aggregate's current highest,
        which together with the unique constraint on
        (aggregate_id, sequence_number) gives optimistic concurrency: two
        concurrent writers racing on the same aggregate cannot both commit.

        Args:
            event_type: Which event occurred.
            aggregate_type: The kind of aggregate it belongs to.
            aggregate_id: The specific aggregate instance.
            payload: Event-specific data. Serialized to a JSON string because
                SQL Server 2014 has no native JSON type.
            actor_user_id: Who caused it. None means the system or the agent.
            occurred_at: Override the timestamp. Defaults to now, in UTC.

        Returns:
            The event as recorded, including its assigned sequence number.
        """
        next_sequence = self._next_sequence_number(aggregate_id)
        timestamp = occurred_at or datetime.now(UTC)
        event_payload = payload or {}

        row = DomainEvent(
            event_id=new_identifier(),
            event_type=event_type.value,
            aggregate_type=aggregate_type.value,
            aggregate_id=aggregate_id,
            sequence_number=next_sequence,
            # SQL Server 2014 DATETIME columns are naive; store UTC and treat
            # every read as UTC. Ruff's DTZ rules keep the inbound value aware.
            occurred_at=timestamp.replace(tzinfo=None),
            actor_user_id=actor_user_id,
            payload=json.dumps(event_payload, default=str),
        )
        self._session.add(row)
        self._session.flush()

        return self._to_recorded_event(row)

    def read_aggregate_stream(self, aggregate_id: str) -> list[RecordedEvent]:
        """Return every event for one aggregate, oldest first.

        This is the stream a projector replays to rebuild an aggregate's
        current state.
        """
        rows = (
            self._session.execute(
                select(DomainEvent)
                .where(DomainEvent.aggregate_id == aggregate_id)
                .order_by(DomainEvent.sequence_number)
            )
            .scalars()
            .all()
        )
        return [self._to_recorded_event(row) for row in rows]

    def read_all(self, limit: int | None = None) -> list[RecordedEvent]:
        """Return the whole log in occurrence order, oldest first.

        Used by the projection rebuild and by the dashboard activity feed.
        """
        statement = select(DomainEvent).order_by(
            DomainEvent.occurred_at, DomainEvent.sequence_number
        )
        if limit is not None:
            statement = statement.limit(limit)

        rows = self._session.execute(statement).scalars().all()
        return [self._to_recorded_event(row) for row in rows]

    def read_recent(self, limit: int = 20) -> list[RecordedEvent]:
        """Return the most recent events, newest first (dashboard activity)."""
        rows = (
            self._session.execute(
                select(DomainEvent).order_by(DomainEvent.occurred_at.desc()).limit(limit)
            )
            .scalars()
            .all()
        )
        return [self._to_recorded_event(row) for row in rows]

    def read_events_of_type(self, event_type: DomainEventType) -> list[RecordedEvent]:
        """Return every event of one type, oldest first.

        Used to answer questions such as "which applications were closed
        because application X was approved?" without scanning the whole log.
        """
        rows = (
            self._session.execute(
                select(DomainEvent)
                .where(DomainEvent.event_type == event_type.value)
                .order_by(DomainEvent.occurred_at)
            )
            .scalars()
            .all()
        )
        return [self._to_recorded_event(row) for row in rows]

    def count(self) -> int:
        """Total number of events in the log."""
        total = self._session.execute(select(func.count()).select_from(DomainEvent))
        return int(total.scalar_one())

    def _next_sequence_number(self, aggregate_id: str) -> int:
        """Compute the next sequence number for an aggregate."""
        highest = self._session.execute(
            select(func.max(DomainEvent.sequence_number)).where(
                DomainEvent.aggregate_id == aggregate_id
            )
        ).scalar_one_or_none()
        return 1 if highest is None else int(highest) + 1

    @staticmethod
    def _to_recorded_event(row: DomainEvent) -> RecordedEvent:
        """Convert a database row into a decoded RecordedEvent."""
        return RecordedEvent(
            event_id=row.event_id,
            event_type=DomainEventType(row.event_type),
            aggregate_type=AggregateType(row.aggregate_type),
            aggregate_id=row.aggregate_id,
            sequence_number=row.sequence_number,
            occurred_at=row.occurred_at.replace(tzinfo=UTC),
            actor_user_id=row.actor_user_id,
            payload=json.loads(row.payload) if row.payload else {},
        )
