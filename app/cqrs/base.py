"""CQRS primitives: the Command/Query split and the dispatch bus.

Course blueprint section 9.2 requires that state-changing operations and
read operations be separated conceptually *and in implementation*. That
separation is expressed here as two distinct type hierarchies with different
contracts, and is verified by tests/unit/test_architecture_guard.py.

The contract, from docs/ARCHITECTURE.md section 3:

    Command  changes state, returns an identifier, a count of what it
             affected, or nothing; appends events
    Query    reads state, returns a DTO, never mutates, never commits

A command never returns read *data*. A count is not data about a record - it
is how much the write did, which the caller cannot learn any other way and
which no query can answer afterwards, because by then the rows have already
moved. `MarkAllNotificationsReadHandler` returns how many messages it
cleared, so the page can say so. When a screen needs the records themselves,
the controller dispatches a query next.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from sqlalchemy.orm import Session, sessionmaker

if TYPE_CHECKING:
    # Imported for typing only: app.cqrs.cache imports Query from this
    # module, so a runtime import here would be circular.
    from app.cqrs.cache import QueryCache

ResultT = TypeVar("ResultT")

#: The session shared by every dispatch inside one `unit_of_work` block, or
#: None when each dispatch should open its own.
#:
#: A ContextVar rather than a Flask global because this module may not import
#: a framework (CLAUDE.md R2). Each thread gets its own context, so the
#: threaded development server cannot leak one request's session into another.
_SHARED_SESSION: ContextVar[Session | None] = ContextVar(
    "petmatch_shared_session", default=None
)


class Command:
    """A request to change system state.

    Subclasses are plain dataclasses carrying validated input. They contain no
    behaviour; the handler owns the business logic. This is a marker base
    class rather than an ABC precisely because it declares no methods - its
    job is to make the command/query split visible in the type system.
    """


class Query:
    """A request to read system state without changing it.

    A marker base class, for the same reason as Command.
    """


class CommandHandler(ABC, Generic[ResultT]):
    """Executes one command type inside a write transaction."""

    @abstractmethod
    def handle(self, command: Command, session: Session) -> ResultT:
        """Apply the command.

        Args:
            command: The validated request.
            session: An open write transaction owned by the bus.

        Returns:
            An identifier of what was created or changed, a count of the
            records it affected, or None. Never a read DTO - that is what
            queries are for.
        """


class QueryHandler(ABC, Generic[ResultT]):
    """Answers one query type. Must not mutate anything."""

    @abstractmethod
    def handle(self, query: Query, session: Session) -> ResultT:
        """Answer the query.

        Args:
            query: The request.
            session: A session used for reading only. Implementations must not
                add, delete or commit.

        Returns:
            A read DTO shaped for the screen that asked for it.
        """


class HandlerNotRegisteredError(LookupError):
    """Raised when nothing is registered to handle a command or query."""


class QueryMutatedStateError(RuntimeError):
    """Raised when a query handler left pending changes on its session.

    A query that writes is a bug in the handler, not a condition to recover
    from. When each query owned a private session the stray write was simply
    rolled back and nobody found out; inside a shared unit of work that
    silence is not available, because rolling back would also discard the
    committed work of commands dispatched earlier in the same request. The
    contract is therefore enforced loudly instead (CLAUDE.md R2).
    """


class MessageBus:
    """Routes commands and queries to their handlers.

    Commands and queries are registered in separate maps and dispatched by
    separate methods, so a query can never accidentally be sent down the
    write path.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        query_cache: QueryCache | None = None,
    ) -> None:
        """Create an empty bus bound to a session factory.

        Args:
            session_factory: Opens the sessions handlers run in.
            query_cache: Optional cache for the query types it was configured
                with. When absent every query reaches the database, which is
                what the tests and the agent's own tooling want.
        """
        self._session_factory = session_factory
        self._query_cache = query_cache
        self._command_handlers: dict[type[Command], CommandHandler[Any]] = {}
        self._query_handlers: dict[type[Query], QueryHandler[Any]] = {}

    def register_command(
        self, command_type: type[Command], handler: CommandHandler[Any]
    ) -> None:
        """Register the handler for a command type."""
        self._command_handlers[command_type] = handler

    def register_query(
        self, query_type: type[Query], handler: QueryHandler[Any]
    ) -> None:
        """Register the handler for a query type."""
        self._query_handlers[query_type] = handler

    def dispatch_command(self, command: Command) -> Any:  # noqa: ANN401 - see below
        """Execute a command inside a committed transaction.

        The transaction wraps the whole handler, so a command that appends
        several events and updates several projections either applies
        completely or not at all.

        The return type is `Any` because the registry is heterogeneous:
        the handler is found by the command's runtime type, so the result
        type cannot be known statically at this point. Narrowing it would
        mean making `Command` generic in its own result, which would pin
        every command class to a type parameter for no gain at the one
        call site that matters - the controller, which knows what it sent.

        Args:
            command: The command to execute.

        Returns:
            Whatever the registered handler returns: an identifier, a count,
            or None.

        Raises:
            HandlerNotRegisteredError: If no handler is registered.
        """
        handler = self._command_handlers.get(type(command))
        if handler is None:
            raise HandlerNotRegisteredError(f"No command handler for {type(command).__name__}")

        shared_session = _SHARED_SESSION.get()
        if shared_session is not None:
            return self._run_command(handler, command, shared_session)

        session = self._session_factory()
        try:
            return self._run_command(handler, command, session)
        finally:
            session.close()

    def _run_command(
        self, handler: CommandHandler[Any], command: Command, session: Session
    ) -> Any:  # noqa: ANN401 - heterogeneous registry, see dispatch_command
        """Run one command handler and commit it, or roll back and re-raise.

        Empties the query cache once the write is durable, so no screen can
        show an answer from before a change the user just made.

        Args:
            handler: The registered handler.
            command: The command to apply.
            session: The session to run in. Closing it, if it needs closing,
                belongs to whoever opened it.

        Returns:
            Whatever the handler returned.
        """
        try:
            result = handler.handle(command, session)
            session.commit()
        except Exception:
            session.rollback()
            raise

        if self._query_cache is not None:
            self._query_cache.invalidate_all()
        return result

    def dispatch_query(self, query: Query) -> Any:  # noqa: ANN401 - see below
        """Answer a query using a read-only session.

        The session is rolled back rather than committed, which makes an
        accidental write in a query handler impossible to persist.

        `Any` for the same reason as `dispatch_command`: the handler is
        resolved from the query's runtime type.

        Args:
            query: The query to answer.

        Returns:
            The read DTO produced by the registered handler.

        Raises:
            HandlerNotRegisteredError: If no handler is registered.
        """
        handler = self._query_handlers.get(type(query))
        if handler is None:
            raise HandlerNotRegisteredError(f"No query handler for {type(query).__name__}")

        if self._query_cache is not None:
            was_cached, cached_answer = self._query_cache.lookup(query)
            if was_cached:
                return cached_answer

        shared_session = _SHARED_SESSION.get()
        if shared_session is not None:
            result = handler.handle(query, shared_session)
            self._reject_pending_writes(shared_session, query)
            self._remember(query, result)
            return result

        session = self._session_factory()
        try:
            answer = handler.handle(query, session)
            self._remember(query, answer)
            return answer
        finally:
            # Queries never commit. Rolling back discards anything a handler
            # might have written by mistake, enforcing the read-only contract
            # at runtime rather than by convention.
            session.rollback()
            session.close()

    def _remember(self, query: Query, answer: Any) -> None:  # noqa: ANN401 - see dispatch_query
        """Offer an answer to the cache, which ignores uncacheable types.

        Args:
            query: The query that was answered.
            answer: What the handler returned.
        """
        if self._query_cache is not None:
            self._query_cache.store(query, answer)

    @staticmethod
    def _reject_pending_writes(session: Session, query: Query) -> None:
        """Fail if a query handler left anything to be written.

        Reads the session's pending state, which is held in memory, so this
        costs no database round trip. The pending work is discarded before
        raising so the shared session stays usable for the rest of the
        request.

        Args:
            session: The shared session the handler just used.
            query: The query that ran, named in the error.

        Raises:
            QueryMutatedStateError: If the handler added, changed or deleted
                anything.
        """
        if not (session.new or session.dirty or session.deleted):
            return
        session.rollback()
        raise QueryMutatedStateError(
            f"{type(query).__name__} left pending changes on the session; "
            "queries must not add, change or delete anything"
        )

    @contextmanager
    def read_only_session(self) -> Iterator[Session]:
        """Lend the unit of work's session for a read the bus cannot route.

        Flask-Login rehydrates the signed-in account on every request through
        a callback that takes a user id, not a query object, so it cannot go
        through `dispatch_query`. Without this it would open a seventh
        connection per page and pay the round trips `unit_of_work` exists to
        avoid.

        Outside a unit of work this opens and closes a private session, so
        the caller behaves the same either way.

        Yields:
            A session to read from. The caller must not write to it.
        """
        shared_session = _SHARED_SESSION.get()
        if shared_session is not None:
            yield shared_session
            return

        session = self._session_factory()
        try:
            yield session
        finally:
            session.rollback()
            session.close()

    @contextmanager
    def unit_of_work(self) -> Iterator[None]:
        """Share one session across every dispatch made inside this block.

        The database is remote (docs/ARCHITECTURE.md §10), and on a measured
        connection every checkout costs a pre-ping round trip and every close
        a rollback round trip. A page that dispatched six times therefore paid
        for six of each before running a single useful query. Inside this
        block it pays once.

        Commands still commit, so work is durable as soon as each one
        finishes; queries still may not write, now enforced by raising rather
        than by a silent rollback. Nesting is allowed and the outermost block
        owns the session.

        Yields:
            None. The scope is ambient: dispatches pick the session up from
            the context, so callers need not thread it through.
        """
        if _SHARED_SESSION.get() is not None:
            yield
            return

        session = self._session_factory()
        token = _SHARED_SESSION.set(session)
        try:
            yield
        finally:
            _SHARED_SESSION.reset(token)
            session.close()
