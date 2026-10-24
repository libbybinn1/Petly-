"""Tests for the bus's session sharing and the read-only contract it enforces.

A unit of work exists for one measured reason: the database is remote, and
every session opened costs round trips - a pre-ping on checkout, a rollback on
close - before any useful query runs. Sharing one session across a request
removes that cost, but only if the sharing is real, so that is what these
prove, along with what the sharing gives up: a query that writes can no longer
be silently rolled back, because rolling back would also discard committed
work. It raises instead.

No Flask application context is built here; the bus is framework-free by rule
R2, and these run against an in-memory SQLite session factory.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from app.cqrs.base import (
    Command,
    CommandHandler,
    MessageBus,
    Query,
    QueryHandler,
    QueryMutatedStateError,
)
from app.cqrs.cache import QueryCache
from app.infrastructure.models import AnalysisJob
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

pytestmark = pytest.mark.unit


# Frozen dataclasses, like every real query: the cache keys on the query's
# value, so two separate asks for the same thing must compare equal.
@dataclass(frozen=True)
class RecordSessionQuery(Query):
    """Asks only which session it was handed."""


@dataclass(frozen=True)
class RecordSessionCommand(Command):
    """Asks only which session it was handed."""


@dataclass(frozen=True)
class WritingQuery(Query):
    """A deliberately misbehaving query that adds a row."""


class SessionRecordingQueryHandler(QueryHandler[Session]):
    """Returns the session it was given, so a test can compare identities."""

    def handle(self, query: Query, session: Session) -> Session:
        """Return the session itself."""
        return session


class SessionRecordingCommandHandler(CommandHandler[Session]):
    """Returns the session it was given.

    A real command may not return read data; this one exists only to let a
    test see which session the bus opened for it.
    """

    def handle(self, command: Command, session: Session) -> Session:
        """Return the session itself."""
        return session


class WritingQueryHandler(QueryHandler[None]):
    """Breaks the contract by staging a row on the session.

    `session.add` populates `session.new` without any flush, so this stages a
    write without needing the table to exist.
    """

    def handle(self, query: Query, session: Session) -> None:
        """Stage a row the query had no business staging."""
        session.add(AnalysisJob(analysis_job_id="should-never-persist"))


def _bus_and_engine(query_cache: QueryCache | None = None) -> tuple[MessageBus, Engine]:
    """Build a bus over an in-memory database, returning its engine too.

    The engine is handed back so a test can ask the connection pool whether
    anything was left checked out.
    """
    # QueuePool rather than the default SingletonThreadPool that SQLite picks
    # for an in-memory database: only QueuePool reports what is checked out,
    # which is how the leak test asks whether the session was really closed.
    engine = create_engine("sqlite://", poolclass=QueuePool)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    bus = MessageBus(session_factory, query_cache)
    bus.register_query(RecordSessionQuery, SessionRecordingQueryHandler())
    bus.register_query(WritingQuery, WritingQueryHandler())
    bus.register_command(RecordSessionCommand, SessionRecordingCommandHandler())
    return bus, engine


def _bus(query_cache: QueryCache | None = None) -> MessageBus:
    """Build a bus over an in-memory database with the handlers registered."""
    bus, _engine = _bus_and_engine(query_cache)
    return bus


def _checked_out(engine: Engine) -> int:
    """How many connections the pool currently has handed out.

    `Engine.pool` is typed as the base `Pool`, which does not declare
    `checkedout`; the isinstance narrows it to the implementation that does.
    """
    pool = engine.pool
    assert isinstance(pool, QueuePool)
    return pool.checkedout()


def test_every_dispatch_inside_one_unit_of_work_shares_a_session() -> None:
    """Proves the sharing is real, which is the entire performance win.

    If each dispatch still opened its own session the page would still pay a
    checkout and a close per query, and the change would have bought nothing.
    """
    bus = _bus()

    with bus.unit_of_work():
        first = bus.dispatch_query(RecordSessionQuery())
        second = bus.dispatch_query(RecordSessionQuery())
        commanded = bus.dispatch_command(RecordSessionCommand())

    assert first is second
    assert first is commanded


def test_dispatches_outside_a_unit_of_work_still_get_their_own_session() -> None:
    """Proves the bus is unchanged for callers that never open a scope.

    The agent process and the scripts dispatch without one, and must keep
    getting an independent session each time.
    """
    bus = _bus()

    first = bus.dispatch_query(RecordSessionQuery())
    second = bus.dispatch_query(RecordSessionQuery())

    assert first is not second


def test_a_query_that_writes_inside_a_unit_of_work_is_rejected() -> None:
    """Proves the read-only contract still holds when the session is shared. The negative case.

    With a private session the stray write was quietly rolled back. Inside a
    shared unit of work that is not available - the rollback would discard
    commands committed earlier in the same request - so the bug is raised
    instead of hidden.
    """
    bus = _bus()

    with bus.unit_of_work(), pytest.raises(QueryMutatedStateError, match="WritingQuery"):
        bus.dispatch_query(WritingQuery())


def test_a_rejected_query_leaves_the_shared_session_usable() -> None:
    """Proves one misbehaving handler does not poison the rest of the request.

    The pending row is discarded before the error is raised, so a later
    dispatch in the same scope still works.
    """
    bus = _bus()

    with bus.unit_of_work():
        with pytest.raises(QueryMutatedStateError):
            bus.dispatch_query(WritingQuery())

        session = bus.dispatch_query(RecordSessionQuery())
        assert not session.new


def test_a_query_that_writes_outside_a_unit_of_work_is_still_rolled_back() -> None:
    """Proves the original behaviour is untouched where no scope is open.

    Nothing persists, and no error is raised, exactly as before.
    """
    bus = _bus()

    bus.dispatch_query(WritingQuery())


def test_a_nested_unit_of_work_reuses_the_outer_session() -> None:
    """Proves nesting is safe, so a helper may open a scope without knowing who called it.

    The inner block must not close the session the outer block is still
    using, or every dispatch after it would fail.
    """
    bus = _bus()

    with bus.unit_of_work():
        outer = bus.dispatch_query(RecordSessionQuery())
        with bus.unit_of_work():
            inner = bus.dispatch_query(RecordSessionQuery())
        after = bus.dispatch_query(RecordSessionQuery())

    assert outer is inner
    assert outer is after


def test_the_shared_session_is_closed_even_when_the_block_raises() -> None:
    """Proves a failing request still returns its connection to the pool.

    A leaked connection is worse than a slow page: Somee's free tier allows
    very few, so leaking them takes the whole site down rather than slowing
    it. The pool is asked directly, because a closed Session still reports
    `is_active` as True and would not catch a leak.
    """
    bus, engine = _bus_and_engine()

    with pytest.raises(RuntimeError), bus.unit_of_work():
        session = bus.dispatch_query(RecordSessionQuery())
        # A Session acquires its connection lazily, so it has to read
        # something before there is anything to leak.
        session.execute(text("SELECT 1"))
        assert _checked_out(engine) == 1
        raise RuntimeError("the view blew up")

    assert _checked_out(engine) == 0


def test_a_command_empties_the_query_cache() -> None:
    """Proves a write cannot leave a stale answer behind for the next page.

    Without this a user who changed something would be shown the value from
    before their own change, which reads as the application ignoring them.
    """
    cache = QueryCache({RecordSessionQuery: 60.0})
    bus, _engine = _bus_and_engine(cache)

    bus.dispatch_query(RecordSessionQuery())
    assert cache.lookup(RecordSessionQuery())[0] is True

    bus.dispatch_command(RecordSessionCommand())

    assert cache.lookup(RecordSessionQuery())[0] is False
