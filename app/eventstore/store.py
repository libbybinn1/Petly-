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
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.enums import AggregateType, DomainEventType
from app.infrastructure.clock import as_aware_utc, as_naive_utc, aware_utc_now
from app.infrastructure.models import DomainEvent, new_identifier

logger = logging.getLogger("petmatch.eventstore")


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
        timestamp = occurred_at or aware_utc_now()
        event_payload = payload or {}

        row = DomainEvent(
            event_id=new_identifier(),
            event_type=event_type.value,
            aggregate_type=aggregate_type.value,
            aggregate_id=aggregate_id,
            sequence_number=next_sequence,
            # SQL Server 2014 DATETIME columns are naive; store UTC and treat
            # every read as UTC. Ruff's DTZ rules keep the inbound value aware,
            # and `as_naive_utc` converts before it strips - a caller passing
            # an aware non-UTC `occurred_at` would otherwise store the clock
            # face rather than the instant.
            occurred_at=as_naive_utc(timestamp),
            actor_user_id=actor_user_id,
            payload=_serialise_payload(event_payload),
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

    def read_all(
        self,
        limit: int | None = None,
        aggregate_types: Sequence[AggregateType] | None = None,
    ) -> list[RecordedEvent]:
        """Return the log in occurrence order, oldest first.

        Args:
            limit: At most this many events. None reads every one.
            aggregate_types: Narrow to these kinds of aggregate. The filter
                belongs in SQL rather than in the caller: the projection
                rebuild replays two of the four kinds and used to read the
                entire log across the network to throw most of it away, and
                the log is the one table in this schema that only grows.

        Returns:
            The matching events, oldest first.
        """
        statement = select(DomainEvent).order_by(
            DomainEvent.occurred_at, DomainEvent.sequence_number
        )
        if aggregate_types is not None:
            statement = statement.where(
                DomainEvent.aggregate_type.in_(
                    [aggregate_type.value for aggregate_type in aggregate_types]
                )
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
            occurred_at=as_aware_utc(row.occurred_at),
            actor_user_id=row.actor_user_id,
            payload=json.loads(row.payload) if row.payload else {},
        )


# The types `json.dumps` writes without help. Anything else reaches
# `default=str` and is stored as its `str()`, which is lossy in ways nothing
# downstream can detect: a Decimal becomes "2.5" and a frozenset becomes
# "frozenset({...})", and both read back as strings for ever.
JSON_NATIVE_TYPES = (str, int, float, bool, type(None))


def _serialise_payload(payload: dict[str, Any]) -> str:
    """Serialise one event payload, warning about anything coerced.

    `default=str` is kept: refusing to append an event because one payload
    value was a Decimal would lose the event, which is worse than storing a
    slightly lossy copy of it. But a silent coercion is how a payload field
    quietly becomes a string that a later reader compares against a number
    and never matches, so each one is named in the log.

    Args:
        payload: The event-specific data.

    Returns:
        The JSON string for the NVARCHAR(MAX) column.
    """
    for key, value in payload.items():
        _warn_about_coercion(key, value)
    return json.dumps(payload, default=str)


def _warn_about_coercion(key: str, value: object, depth: int = 0) -> None:
    """Log a warning for a value `json.dumps` cannot write natively."""
    if isinstance(value, JSON_NATIVE_TYPES):
        return

    if isinstance(value, dict) and depth < MAXIMUM_PAYLOAD_DEPTH:
        for nested_key, nested_value in value.items():
            _warn_about_coercion(f"{key}.{nested_key}", nested_value, depth + 1)
        return

    if isinstance(value, list) and depth < MAXIMUM_PAYLOAD_DEPTH:
        for index, item in enumerate(value):
            _warn_about_coercion(f"{key}[{index}]", item, depth + 1)
        return

    logger.warning(
        "event payload key %r holds a %s, which is stored as its str(); "
        "it will read back as text",
        key,
        type(value).__name__,
    )


# Deep enough for any payload this system writes, and shallow enough that a
# self-referential structure cannot spin here.
MAXIMUM_PAYLOAD_DEPTH = 4
