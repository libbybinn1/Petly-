"""CQRS primitives: the Command/Query split and the dispatch bus.

Course blueprint section 9.2 requires that state-changing operations and
read operations be separated conceptually *and in implementation*. That
separation is expressed here as two distinct type hierarchies with different
contracts, and is verified by tests/unit/test_cqrs_separation.py.

The contract, from docs/ARCHITECTURE.md section 3:

    Command  changes state, returns an identifier or nothing, appends events
    Query    reads state, returns a DTO, never mutates, never commits

A command never returns read data. When a screen needs data after a write,
the controller dispatches a query next.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Generic, TypeVar

from sqlalchemy.orm import Session, sessionmaker

ResultT = TypeVar("ResultT")


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
            An identifier of what was created or changed, or None. Never a
            read DTO - that is what queries are for.
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


class MessageBus:
    """Routes commands and queries to their handlers.

    Commands and queries are registered in separate maps and dispatched by
    separate methods, so a query can never accidentally be sent down the
    write path.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        """Create an empty bus bound to a session factory."""
        self._session_factory = session_factory
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
            Whatever the registered handler returns: an identifier, or None.

        Raises:
            HandlerNotRegisteredError: If no handler is registered.
        """
        handler = self._command_handlers.get(type(command))
        if handler is None:
            raise HandlerNotRegisteredError(f"No command handler for {type(command).__name__}")

        session = self._session_factory()
        try:
            result = handler.handle(command, session)
            session.commit()
            return result
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

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

        session = self._session_factory()
        try:
            return handler.handle(query, session)
        finally:
            # Queries never commit. Rolling back discards anything a handler
            # might have written by mistake, enforcing the read-only contract
            # at runtime rather than by convention.
            session.rollback()
            session.close()
