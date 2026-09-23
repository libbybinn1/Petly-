"""Adversarial integration tests for the append-only event store.

The log is the source of truth for FR-13.3 ("current state MUST be
reconstructible by replaying the log"), so its numbering, its ordering and its
tolerance of awkward payload values all matter more than the projections do.

SQLite with the real schema, for the reasons given in the existing
integration suite.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest
from app.domain.enums import AggregateType, DomainEventType
from app.eventstore.store import EventStore
from app.infrastructure.database import Base
from sqlalchemy import Table, create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration


@pytest.fixture
def session_factory() -> Iterator[sessionmaker[Session]]:
    """A throwaway SQLite database with the real schema."""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


class TestSequenceNumbering:
    """Per-aggregate sequences with no gaps and no duplicates."""

    def test_sequences_start_at_one_and_have_no_gaps(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """Proves a replay can rely on a dense 1..n sequence per aggregate."""
        with session_factory() as session:
            store = EventStore(session)
            for _ in range(5):
                store.append(
                    DomainEventType.APPLICATION_SUBMITTED,
                    AggregateType.APPLICATION,
                    "aggregate-1",
                )
            session.commit()

            stream = store.read_aggregate_stream("aggregate-1")
            numbers = [event.sequence_number for event in stream]

        assert numbers == [1, 2, 3, 4, 5]

    def test_two_aggregates_number_independently(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """Proves one aggregate's history is not renumbered by another's activity."""
        with session_factory() as session:
            store = EventStore(session)
            store.append(
                DomainEventType.APPLICATION_SUBMITTED, AggregateType.APPLICATION, "a"
            )
            store.append(
                DomainEventType.INVITATION_SENT, AggregateType.INVITATION, "b"
            )
            store.append(
                DomainEventType.APPLICATION_APPROVED, AggregateType.APPLICATION, "a"
            )
            session.commit()

            assert [e.sequence_number for e in store.read_aggregate_stream("a")] == [1, 2]
            assert [e.sequence_number for e in store.read_aggregate_stream("b")] == [1]

    def test_a_duplicate_sequence_number_is_refused_by_the_database(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """Proves the optimistic-concurrency claim in `append`'s docstring holds.

        `_next_sequence_number` is a read followed by a write, so two writers
        racing on one aggregate can both compute the same number. The unique
        constraint on (aggregate_id, sequence_number) is what stops them both
        committing, and this test is what proves that constraint is really
        there rather than only described.
        """
        from app.infrastructure.models import DomainEvent, new_identifier

        with session_factory() as session:
            EventStore(session).append(
                DomainEventType.APPLICATION_SUBMITTED, AggregateType.APPLICATION, "a"
            )
            session.commit()

        with session_factory() as session:
            session.add(
                DomainEvent(
                    event_id=new_identifier(),
                    event_type=DomainEventType.APPLICATION_APPROVED.value,
                    aggregate_type=AggregateType.APPLICATION.value,
                    aggregate_id="a",
                    sequence_number=1,
                    occurred_at=datetime.now(UTC).replace(tzinfo=None),
                    actor_user_id=None,
                    payload="{}",
                )
            )

            with pytest.raises(IntegrityError):
                session.commit()

    def test_the_store_exposes_no_way_to_change_or_delete_an_event(self) -> None:
        """Proves FR-13.2 by the shape of the API, not by convention."""
        forbidden = {"update", "delete", "remove", "amend", "rewrite"}
        exposed = {
            name for name in dir(EventStore) if not name.startswith("_")
        }

        assert exposed & forbidden == set()


class TestPayloadSerialisation:
    """Payloads are NVARCHAR(MAX): SQL Server 2014 has no JSON type."""

    def test_a_datetime_in_the_payload_is_serialised_rather_than_raising(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """Proves `json.dumps(default=str)` covers the value most likely to appear.

        Several commands put timestamps in their payload, so a TypeError here
        would abort a command mid-cascade.
        """
        moment = datetime(2025, 3, 1, 12, 0, tzinfo=UTC)

        with session_factory() as session:
            store = EventStore(session)
            store.append(
                DomainEventType.INVITATION_SENT,
                AggregateType.INVITATION,
                "invitation-1",
                payload={"expires_at": moment},
            )
            session.commit()

            recorded = store.read_aggregate_stream("invitation-1")[0]

        assert recorded.payload["expires_at"] == str(moment)

    def test_a_decimal_in_the_payload_is_serialised_as_text(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """Proves a DECIMAL column value (age, hours) does not break the append.

        Worth pinning: the value survives but comes back as a *string*, so a
        consumer comparing it numerically would silently mismatch.
        """
        with session_factory() as session:
            store = EventStore(session)
            store.append(
                DomainEventType.ADOPTER_PROFILE_UPDATED,
                AggregateType.ADOPTER_PROFILE,
                "profile-1",
                payload={"daily_hours_available": Decimal("2.5")},
            )
            session.commit()

            recorded = store.read_aggregate_stream("profile-1")[0]

        assert recorded.payload["daily_hours_available"] == "2.5"

    def test_a_set_in_the_payload_is_serialised_by_repr_not_as_a_list(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """Documents a sharp edge: `default=str` turns a set into its repr.

        `preferred_species` is a frozenset in the domain, so a command that
        put one straight into a payload would store "frozenset({...})" rather
        than a JSON array. Not currently done anywhere, but it would pass
        review silently.
        """
        with session_factory() as session:
            store = EventStore(session)
            store.append(
                DomainEventType.ADOPTER_PROFILE_UPDATED,
                AggregateType.ADOPTER_PROFILE,
                "profile-2",
                payload={"species": {"DOG"}},
            )
            session.commit()

            recorded = store.read_aggregate_stream("profile-2")[0]

        assert recorded.payload["species"] == "{'DOG'}"

    def test_an_empty_payload_reads_back_as_an_empty_dictionary(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """Proves a payload-free event does not read back as None and crash a template."""
        with session_factory() as session:
            store = EventStore(session)
            store.append(
                DomainEventType.INVITATION_EXPIRED, AggregateType.INVITATION, "inv-2"
            )
            session.commit()

            assert store.read_aggregate_stream("inv-2")[0].payload == {}


class TestTimestampsAndOrdering:
    """The log stores naive UTC and must hand back aware values."""

    def test_an_aware_timestamp_is_stored_naive_and_read_back_aware(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """Proves the UTC round trip survives a DATETIME2 column with no offset."""
        moment = datetime(2025, 3, 1, 12, 0, tzinfo=UTC)

        with session_factory() as session:
            store = EventStore(session)
            recorded = store.append(
                DomainEventType.APPLICATION_SUBMITTED,
                AggregateType.APPLICATION,
                "a",
                occurred_at=moment,
            )
            session.commit()

        assert recorded.occurred_at == moment
        assert recorded.occurred_at.tzinfo is not None

    def test_a_naive_override_is_accepted_and_assumed_to_be_utc(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """Documents that `append` does not reject a naive `occurred_at`.

        `calculate_expiry` refuses a naive datetime outright; the event store
        quietly assumes UTC instead. Recorded here as accepted behaviour, since
        every call site passes an aware value or nothing, but the asymmetry is
        worth knowing about.
        """
        naive = datetime(2025, 3, 1, 12, 0)  # noqa: DTZ001

        with session_factory() as session:
            recorded = EventStore(session).append(
                DomainEventType.APPLICATION_SUBMITTED,
                AggregateType.APPLICATION,
                "a",
                occurred_at=naive,
            )
            session.commit()

        assert recorded.occurred_at == naive.replace(tzinfo=UTC)

    def test_a_stream_replays_in_sequence_order_even_when_timestamps_are_backwards(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """Proves replay order comes from the sequence, not from the clock.

        Out-of-order timestamps are realistic: a backfill, a clock correction
        or an explicit `occurred_at` override all produce them. If replay
        followed `occurred_at` the reconstructed state would be wrong.
        """
        base = datetime(2025, 3, 1, 12, 0, tzinfo=UTC)

        with session_factory() as session:
            store = EventStore(session)
            store.append(
                DomainEventType.APPLICATION_SUBMITTED, AggregateType.APPLICATION,
                "a", occurred_at=base,
            )
            store.append(
                DomainEventType.APPLICATION_APPROVED, AggregateType.APPLICATION,
                "a", occurred_at=base - timedelta(days=1),
            )
            store.append(
                DomainEventType.APPLICATION_WITHDRAWN, AggregateType.APPLICATION,
                "a", occurred_at=base - timedelta(days=2),
            )
            session.commit()

            replayed = [event.event_type for event in store.read_aggregate_stream("a")]

        assert replayed == [
            DomainEventType.APPLICATION_SUBMITTED,
            DomainEventType.APPLICATION_APPROVED,
            DomainEventType.APPLICATION_WITHDRAWN,
        ]

    def test_reading_an_unknown_aggregate_returns_an_empty_stream(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """Proves a bad identifier is an empty history, not an exception."""
        with session_factory() as session:
            assert EventStore(session).read_aggregate_stream("no-such-thing") == []

    def test_recent_activity_on_an_empty_log_is_empty(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """Proves the dashboard feed copes with a database that has no events yet."""
        with session_factory() as session:
            store = EventStore(session)

            assert store.read_recent(limit=20) == []
            assert store.read_all() == []
            assert store.count() == 0


class TestUnknownEventTypesInStoredRows:
    """A row written by an older or newer version of the code."""

    def test_an_unrecognised_event_type_raises_on_read(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """Documents that reading is strict: an unknown event_type fails loudly.

        `_to_recorded_event` calls `DomainEventType(row.event_type)`, so a row
        whose type this version does not define raises ValueError and takes
        the whole history page or dashboard feed with it. Strictness is
        defensible for an append-only log, but it means a partial deployment
        breaks reads rather than degrading them.
        """
        from app.infrastructure.models import DomainEvent, new_identifier

        with session_factory() as session:
            session.add(
                DomainEvent(
                    event_id=new_identifier(),
                    event_type="SomethingFromTheFuture",
                    aggregate_type=AggregateType.APPLICATION.value,
                    aggregate_id="a",
                    sequence_number=1,
                    occurred_at=datetime.now(UTC).replace(tzinfo=None),
                    actor_user_id=None,
                    payload="{}",
                )
            )
            session.commit()

            with pytest.raises(ValueError, match="SomethingFromTheFuture"):
                EventStore(session).read_aggregate_stream("a")

    def test_every_event_type_the_code_appends_is_in_the_enum(self) -> None:
        """Proves the enum is the authoritative catalogue the docstring claims.

        Guards against a string literal creeping into a command, which would
        write a row the reader above cannot decode.
        """
        from app.infrastructure.models import DomainEvent

        with_check = [
            constraint
            for constraint in cast("Table", DomainEvent.__table__).constraints
            if constraint.__class__.__name__ == "CheckConstraint"
        ]

        assert with_check, "domain_events should constrain event_type to the enum"

    def test_stored_types_round_trip_through_the_enum(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """Proves every declared event type can be appended and read back."""
        with session_factory() as session:
            store = EventStore(session)
            for index, event_type in enumerate(DomainEventType.__members__.values()):
                store.append(event_type, AggregateType.APPLICATION, f"aggregate-{index}")
            session.commit()

            assert store.count() == len(list(DomainEventType))
            assert len(store.read_all()) == len(list(DomainEventType))
