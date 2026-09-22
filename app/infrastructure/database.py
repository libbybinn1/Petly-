"""SQLAlchemy engine, session factory and the declarative base.

This module owns database connectivity. Per rule R2, it and the repositories
are the only places permitted to hold a session.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import Configuration

# SQL Server requires constraint names to be unique across the whole database,
# not merely within a table. Several tables share column names such as
# `status` and `activity_level`, so hand-written names collide. This
# convention prefixes every generated name with its table, which also gives
# migrations stable, predictable identifiers to reference.
CONSTRAINT_NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for every ORM model."""

    metadata = MetaData(naming_convention=CONSTRAINT_NAMING_CONVENTION)


def create_database_engine(configuration: Configuration) -> Engine:
    """Create the SQLAlchemy engine for the configured database.

    Somee.com is a shared host whose connections are occasionally refused or
    slow, so `pool_pre_ping` verifies a connection before handing it out and
    `pool_recycle` discards ones the server may have dropped. Putting the retry
    behaviour here keeps it out of individual call sites.

    Args:
        configuration: Loaded application configuration.

    Returns:
        A configured engine. Connections are opened lazily.
    """
    url = configuration.sqlalchemy_url
    is_sqlite = url.startswith("sqlite")

    if is_sqlite:
        return create_engine(url, echo=False, future=True)

    return create_engine(
        url,
        echo=False,
        future=True,
        pool_pre_ping=True,
        pool_recycle=280,
        pool_size=5,
        max_overflow=5,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Build the session factory bound to an engine."""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """Provide a transactional scope around a series of operations.

    Commits on success, rolls back on any exception, and always closes. Command
    handlers use this so a partially-applied state change can never be
    committed.

    Yields:
        An open session bound to a transaction.
    """
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
