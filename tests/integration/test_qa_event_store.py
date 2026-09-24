"""Adversarial integration tests for the append-only event store.

The log is the source of truth for FR-13.3 ("current state MUST be
reconstructible by replaying the log"), so its numbering, its ordering and its
tolerance of awkward payload values all matter more than the projections do.

SQLite with the real schema, for the reasons given in the existing
integration suite.
"""

from __future__ import annotations

import ast
import logging
import pathlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import uuid4

import pytest
from app.domain.enums import AggregateType, DomainEventType, MatchDirection
from app.eventstore.store import EventStore
from app.infrastructure.database import Base
from app.infrastructure.models import MatchAnalysis
from sqlalchemy import CheckConstraint, Table, create_engine
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


def _append_calls(tree: ast.Module) -> list[ast.Call]:
    """Every `<something>.append(...)` call in a parsed module.

    Matched on the method name rather than on the receiver: a handler may
    reach the store through `event_store`, `EventStore(session)` or a local
    name, and all three are the same call.
    """
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "append"
        and node.args
        and not isinstance(node.args[0], ast.Constant)
    ]


def _names_a_domain_event_type(node: ast.expr | None) -> bool:
    """Whether an argument names a `DomainEventType` member.

    A conditional counts when both of its branches do: one handler picks
    between accepted and declined inline, and spelling that as two calls
    would be worse code for the sake of a simpler rule.
    """
    if isinstance(node, ast.IfExp):
        return _names_a_domain_event_type(node.body) and _names_a_domain_event_type(
            node.orelse
        )
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "DomainEventType"
    )


def _member_names_in(node: ast.expr) -> list[str]:
    """The `DomainEventType` member names one argument can evaluate to."""
    if isinstance(node, ast.IfExp):
        return _member_names_in(node.body) + _member_names_in(node.orelse)
    assert isinstance(node, ast.Attribute)
    return [node.attr]


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

    def test_a_coerced_payload_value_is_logged_by_key_and_type(
        self,
        session_factory: sessionmaker[Session],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Proves a lossy coercion leaves a trace naming what was lost.

        The coercion itself is kept - refusing the append would lose the
        event, which is worse - but it used to happen in silence, so a field
        that quietly became a string was discovered by a later reader
        comparing it against a number and never matching.
        """
        with (
            caplog.at_level(logging.WARNING, logger="petmatch.eventstore"),
            session_factory() as session,
        ):
                EventStore(session).append(
                    DomainEventType.ADOPTER_PROFILE_UPDATED,
                    AggregateType.ADOPTER_PROFILE,
                    "profile-1",
                    payload={"daily_hours_available": Decimal("2.5")},
                )
                session.commit()

        messages = [record.getMessage() for record in caplog.records]

        assert any(
            "daily_hours_available" in message and "Decimal" in message
            for message in messages
        )

    def test_an_ordinary_payload_logs_nothing(
        self,
        session_factory: sessionmaker[Session],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Proves the warning marks the exception rather than every append.

        Negative half of the pair: a warning on every event would be noise
        nobody reads, which is the same as no warning at all.
        """
        with (
            caplog.at_level(logging.WARNING, logger="petmatch.eventstore"),
            session_factory() as session,
        ):
                EventStore(session).append(
                    DomainEventType.ANIMAL_LISTED,
                    AggregateType.ANIMAL,
                    "animal-1",
                    payload={"animal_id": "animal-1", "score": 80, "listed": True},
                )
                session.commit()

        assert caplog.records == []

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

    def test_an_unrecognised_event_type_cannot_be_written(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """Proves the database refuses a type the enum does not define.

        `DomainEventType` calls itself the authoritative catalogue, and only
        `aggregate_type` was holding the database to that. A row whose
        `event_type` names no member cannot be read back at all - see the
        test below - so the write is what has to be refused, and only the
        database can refuse a write that did not come through the ORM.
        """
        from app.infrastructure.models import DomainEvent, new_identifier

        with session_factory() as session, pytest.raises(IntegrityError):
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

    def test_an_unrecognised_event_type_also_raises_on_read(self) -> None:
        """Documents that reading is strict as well as writing.

        Read off a detached row rather than a stored one, because the column
        no longer accepts such a value: this is the behaviour a row written
        by a *newer* deployment would meet, and it is worth recording that
        the reader fails loudly rather than degrading - a partial deployment
        breaks the history page instead of showing it with a gap.
        """
        from app.infrastructure.models import DomainEvent, new_identifier

        row = DomainEvent(
            event_id=new_identifier(),
            event_type="SomethingFromTheFuture",
            aggregate_type=AggregateType.APPLICATION.value,
            aggregate_id="a",
            sequence_number=1,
            occurred_at=datetime.now(UTC).replace(tzinfo=None),
            actor_user_id=None,
            payload="{}",
        )

        with pytest.raises(ValueError, match="SomethingFromTheFuture"):
            EventStore._to_recorded_event(row)

    def test_every_event_type_the_code_appends_is_in_the_enum(self) -> None:
        """Proves every `EventStore.append` names a `DomainEventType` member.

        Guards against a string literal creeping into a command, which would
        write a row the reader above cannot decode. Checked by reading the
        command modules rather than by asserting a CheckConstraint merely
        exists - which the previous version of this test did, and which would
        have passed just as happily with a literal in every handler.
        """
        commands = pathlib.Path("app/cqrs/commands")
        appended: list[str] = []
        problems: list[str] = []

        for module in sorted(commands.rglob("*.py")):
            tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
            for call in _append_calls(tree):
                argument = call.args[0]
                if _names_a_domain_event_type(argument):
                    appended.extend(_member_names_in(argument))
                    continue
                problems.append(
                    f"{module}:{call.lineno} appends {ast.unparse(argument)}"
                )

        assert problems == [], "; ".join(problems)
        assert appended, "no EventStore.append call was found to check"
        for member_name in appended:
            assert hasattr(DomainEventType, member_name), member_name

    def test_the_check_constraint_lists_every_enum_value(self) -> None:
        """Proves the database half of the catalogue is complete.

        A constraint that merely exists is not the claim: it has to name
        every member, or a type the code appends would be refused by the
        database that the enum says is valid.
        """
        from app.infrastructure.models import DomainEvent

        constraints = [
            str(constraint.sqltext)
            for constraint in cast("Table", DomainEvent.__table__).constraints
            if isinstance(constraint, CheckConstraint)
        ]

        constraint_text = " ".join(constraints)

        assert constraints, "domain_events should constrain event_type to the enum"
        for member in DomainEventType:
            assert f"'{member.value}'" in constraint_text, member.value
        for aggregate in AggregateType:
            assert f"'{aggregate.value}'" in constraint_text, aggregate.value

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


class TestReadingTheLogByAggregateKind:
    """`read_all` narrows in SQL rather than leaving the caller to filter.

    The projection rebuild replays two of the four aggregate kinds. It used
    to read the entire log across the network and discard most of it, and the
    log is the one table in this schema that only ever grows.
    """

    def test_only_the_requested_kinds_come_back(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """Proves the filter selects rather than the caller rejecting."""
        with session_factory() as session:
            store = EventStore(session)
            store.append(
                DomainEventType.APPLICATION_SUBMITTED, AggregateType.APPLICATION, "app-1"
            )
            store.append(DomainEventType.ANIMAL_LISTED, AggregateType.ANIMAL, "animal-1")
            store.append(
                DomainEventType.INVITATION_SENT, AggregateType.INVITATION, "invite-1"
            )
            session.commit()

            selected = store.read_all(
                aggregate_types=(AggregateType.APPLICATION, AggregateType.INVITATION)
            )

            assert {event.aggregate_id for event in selected} == {"app-1", "invite-1"}
            assert len(store.read_all()) == 3

    def test_an_unmatched_kind_reads_as_empty(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """Proves the filter is real and not quietly ignored.

        Negative half of the pair: a parameter that narrowed nothing would
        pass the test above just as happily.
        """
        with session_factory() as session:
            store = EventStore(session)
            store.append(DomainEventType.ANIMAL_LISTED, AggregateType.ANIMAL, "animal-1")
            session.commit()

            assert store.read_all(aggregate_types=(AggregateType.APPLICATION,)) == []


class TestOneAnalysisPerPairing:
    """`agent_service.worker` promises an in-place update; the schema enforces it.

    Its find-then-write is not atomic, so two workers claiming the same
    pairing would each find nothing and each insert. The read side would then
    show whichever row it ordered last, beside a score computed from the
    other one.
    """

    @staticmethod
    def _analysis(**overrides: object) -> MatchAnalysis:
        """One analysis row, with the columns the indexes cover."""
        values: dict[str, object] = {
            "match_analysis_id": str(uuid4()),
            "direction": MatchDirection.ANIMAL_TO_ADOPTER.value,
            "adopter_profile_id": "profile-1",
            "animal_id": "animal-1",
            "application_id": None,
            "score": 80,
            "is_disqualified": False,
            "criterion_scores": "[]",
            "reasons": "[]",
            "concerns": "[]",
            "missing_information": "[]",
            "evidence_sources": "[]",
            "used_web_search": False,
            "model_name": "stub",
            "generated_at": datetime.now(UTC).replace(tzinfo=None),
        }
        values.update(overrides)
        return MatchAnalysis(**values)

    def test_a_second_analysis_for_the_same_discovery_is_refused(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """Proves the nullable application_id case is covered.

        This is the case a single four-column unique index would miss: NULL
        is not equal to itself, so any number of rows would pass.
        """
        with session_factory() as session:
            session.add(self._analysis())
            session.commit()

        with session_factory() as session, pytest.raises(IntegrityError) as failure:
            session.add(self._analysis())
            session.commit()

        # SQLite names the columns rather than the index, so the message is
        # matched on what it does say: the three-column form, without
        # application_id, is `uq_analysis_without_application`.
        message = str(failure.value)
        assert "UNIQUE constraint failed" in message
        assert "match_analyses.direction" in message
        assert "match_analyses.application_id" not in message

    def test_a_second_analysis_for_the_same_application_is_refused(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """Proves the applied-for case is covered too."""
        with session_factory() as session:
            session.add(self._analysis(application_id="application-1"))
            session.commit()

        with session_factory() as session, pytest.raises(IntegrityError) as failure:
            session.add(self._analysis(application_id="application-1"))
            session.commit()

        message = str(failure.value)
        assert "UNIQUE constraint failed" in message
        assert "match_analyses.application_id" in message

    def test_the_two_directions_are_stored_separately(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """Proves the rule is per direction, not per pairing.

        The same pair is scored from both sides with different weightings,
        and both results are real. An index that collapsed them would lose
        one of the two screens.
        """
        with session_factory() as session:
            session.add(self._analysis())
            session.add(self._analysis(direction=MatchDirection.ADOPTER_TO_ANIMAL.value))
            session.commit()

            assert session.query(MatchAnalysis).count() == 2
