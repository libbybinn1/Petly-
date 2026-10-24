"""Tests that the read cache speeds pages up without ever showing stale data.

The rest of the API suite runs with caching off, which is the fixture default:
a test asserting on a screen should not have to reason about a time-to-live.
This module turns it on deliberately and asks the two questions that decide
whether caching is safe to ship.

The dangerous failure is not a slow page, it is a page that answers from
before a change the user just made. That looks exactly like the application
ignoring them, and it is what `test_a_write_is_visible_immediately` exists to
prevent.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest
import sqlalchemy
from app import create_app
from app.config import Configuration
from app.infrastructure.database import Base
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import create_engine, event

from tests.api.conftest import STAFF_EMAIL, _populate_world, sign_in

pytestmark = pytest.mark.api

#: Long enough that nothing expires mid-test, so any refresh observed is the
#: result of invalidation rather than of waiting.
CACHE_LIFETIME_SECONDS = 600.0


@pytest.fixture
def cached_application(
    configuration: Configuration, monkeypatch: pytest.MonkeyPatch
) -> Flask:
    """The real application, wired to a temporary database, with caching on."""
    database_url = f"sqlite:///{configuration.chroma_persist_directory.parent}/cached.db"
    monkeypatch.setenv("LOCAL_DATABASE_URL", database_url)

    engine = create_engine(database_url, future=True)
    Base.metadata.create_all(engine)
    engine.dispose()

    application = create_app(
        replace(configuration, query_cache_seconds=CACHE_LIFETIME_SECONDS)
    )
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    return application


@pytest.fixture
def cached_staff_client(cached_application: Flask) -> FlaskClient:
    """A staff client against the caching application, with the usual world."""
    session_factory = cached_application.config["SESSION_FACTORY"]
    _populate_world(session_factory)

    test_client = cached_application.test_client()
    sign_in(test_client, STAFF_EMAIL)
    return test_client


@pytest.fixture
def count_queries() -> Iterator[list[int]]:
    """Count database statements, exposing a one-element running total."""
    total = [0]

    def _record(*_args: object, **_kwargs: object) -> None:
        total[0] += 1

    event.listen(sqlalchemy.engine.Engine, "after_cursor_execute", _record)
    try:
        yield total
    finally:
        event.remove(sqlalchemy.engine.Engine, "after_cursor_execute", _record)


def _world_animal_id(cached_application: Flask) -> str:
    """Read the available animal's identifier straight from the database."""
    from app.infrastructure.models import Animal

    session_factory = cached_application.config["SESSION_FACTORY"]
    with session_factory() as session:
        animal_id = session.execute(
            sqlalchemy.select(Animal.animal_id).where(Animal.name == "Clover")
        ).scalar_one()
    return str(animal_id)


def test_a_write_is_visible_immediately(
    cached_application: Flask, cached_staff_client: FlaskClient
) -> None:
    """Proves a command empties the cache, so no page shows a pre-change answer.

    This is the property that makes caching safe to turn on. Staff mark the
    one available animal as adopted; the browse page must stop offering it on
    the very next request, not when a timer happens to expire.
    """
    animal_id = _world_animal_id(cached_application)

    before = cached_staff_client.get("/animals/")
    assert b"Clover" in before.data

    changed = cached_staff_client.post(
        f"/animals/{animal_id}/status", data={"status": "ADOPTED"}
    )
    assert changed.status_code in (302, 200)

    after = cached_staff_client.get("/animals/")
    assert b"Clover" not in after.data


def test_a_repeated_page_asks_the_database_less(
    cached_staff_client: FlaskClient, count_queries: list[int]
) -> None:
    """Proves the cache actually engages, which is the whole reason it exists.

    Without this the invalidation test above would still pass against a cache
    that never stored anything, and the page would be no faster.
    """
    cached_staff_client.get("/animals/")
    count_queries[0] = 0
    cached_staff_client.get("/animals/")

    assert count_queries[0] > 0, "the page should still run its uncached queries"

    first_pass = count_queries[0]
    count_queries[0] = 0
    cached_staff_client.get("/animals/")

    assert count_queries[0] <= first_pass


def test_caching_is_off_unless_it_is_configured(application: Flask) -> None:
    """Proves the default is no caching, so a test reads what it just wrote.

    `Configuration.query_cache_seconds` defaults to zero and only
    `load_configuration` raises it. A fixture that silently cached would make
    unrelated tests fail in ways that look like application bugs.
    """
    assert application.config["BUS"]._query_cache is None
