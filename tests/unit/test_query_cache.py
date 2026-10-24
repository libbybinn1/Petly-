"""Tests for the read cache that spares the remote database repeated questions.

A cache is only worth having if it can be trusted, so most of what is proved
here is when it declines to answer: for a query type nobody opted in, after a
write, and once an entry is older than its time-to-live. The clock is injected
rather than slept on, so expiry is tested in microseconds.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from app.cqrs.base import Query
from app.cqrs.cache import QueryCache

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class CachedQuery(Query):
    """A query type the cache has been told it may keep."""

    value: str


@dataclass(frozen=True)
class UncachedQuery(Query):
    """A query type absent from the policy map."""

    value: str


class FakeClock:
    """A monotonic clock that only moves when a test moves it."""

    def __init__(self) -> None:
        """Start at zero."""
        self.now = 0.0

    def __call__(self) -> float:
        """Return the current reading, in seconds."""
        return self.now


def _cache(ttl_seconds: float = 10.0) -> tuple[QueryCache, FakeClock]:
    """Build a cache holding `CachedQuery` only, with a controllable clock."""
    clock = FakeClock()
    return QueryCache({CachedQuery: ttl_seconds}, monotonic=clock), clock


def test_a_stored_answer_is_returned_within_its_lifetime() -> None:
    """Proves the cache answers a repeat of the same query without the caller asking again.

    This is the whole point: on a remote database the second ask would
    otherwise cost another round trip.
    """
    cache, _clock = _cache()
    cache.store(CachedQuery(value="x"), "answer")

    assert cache.lookup(CachedQuery(value="x")) == (True, "answer")


def test_an_entry_is_dropped_once_it_is_older_than_its_lifetime() -> None:
    """Proves a stale entry is a miss, bounding how old an answer can be.

    The agent writes from a separate OS process and cannot clear this cache,
    so the time-to-live is the only limit on staleness from that direction.
    """
    cache, clock = _cache(ttl_seconds=10.0)
    cache.store(CachedQuery(value="x"), "answer")

    clock.now = 10.0

    assert cache.lookup(CachedQuery(value="x")) == (False, None)


def test_an_entry_survives_right_up_to_its_expiry() -> None:
    """Proves the lifetime is honoured fully, not rounded away a moment early."""
    cache, clock = _cache(ttl_seconds=10.0)
    cache.store(CachedQuery(value="x"), "answer")

    clock.now = 9.999

    assert cache.lookup(CachedQuery(value="x")) == (True, "answer")


def test_a_query_type_nobody_opted_in_is_never_stored() -> None:
    """Proves caching is opt-in. The safety property.

    Query types are added to the policy map deliberately. The analysis-status
    endpoint is deliberately absent, because it exists to notice work another
    process finished; caching it by accident would make the Find My Pet page
    poll for ever against a frozen answer.
    """
    cache, _clock = _cache()
    cache.store(UncachedQuery(value="x"), "answer")

    assert cache.lookup(UncachedQuery(value="x")) == (False, None)


def test_a_cached_none_is_a_hit_rather_than_a_miss() -> None:
    """Proves a query that legitimately answers None is still served from the cache.

    Returning the answer alone would make None indistinguishable from "not
    stored", so such a query would re-run on every single request - exactly
    the queries most worth caching, since None usually means "nothing found"
    after a full scan.
    """
    cache, _clock = _cache()
    cache.store(CachedQuery(value="x"), None)

    assert cache.lookup(CachedQuery(value="x")) == (True, None)


def test_queries_differing_only_in_their_fields_do_not_share_an_entry() -> None:
    """Proves the key is the query's value, so one animal's answer cannot be served for another."""
    cache, _clock = _cache()
    cache.store(CachedQuery(value="first"), "first answer")

    assert cache.lookup(CachedQuery(value="second")) == (False, None)
    assert cache.lookup(CachedQuery(value="first")) == (True, "first answer")


def test_invalidating_forgets_everything() -> None:
    """Proves a write empties the cache, so no screen shows a pre-change answer.

    The bus calls this after every command that commits. Total invalidation is
    deliberate: naming the queries each command affects would be a list that
    goes wrong the first time somebody adds a handler and forgets.
    """
    cache, _clock = _cache()
    cache.store(CachedQuery(value="x"), "answer")

    cache.invalidate_all()

    assert cache.lookup(CachedQuery(value="x")) == (False, None)


def test_hit_and_miss_counts_describe_what_happened() -> None:
    """Proves the counters can be trusted when judging whether caching is helping."""
    cache, _clock = _cache()

    cache.lookup(CachedQuery(value="x"))
    cache.store(CachedQuery(value="x"), "answer")
    cache.lookup(CachedQuery(value="x"))
    cache.lookup(CachedQuery(value="x"))

    assert (cache.hit_count, cache.miss_count) == (2, 1)


def test_an_uncacheable_lookup_is_not_counted_as_a_miss() -> None:
    """Proves the counters measure the cache's usefulness, not traffic it never claimed.

    Counting a query the cache was never asked to keep would make the hit rate
    look worse the more uncached queries the application ran.
    """
    cache, _clock = _cache()

    cache.lookup(UncachedQuery(value="x"))

    assert (cache.hit_count, cache.miss_count) == (0, 0)
