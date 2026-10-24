"""A short-lived cache for read queries whose answers tolerate brief staleness.

The database is remote and shared-hosted, and a round trip to it costs roughly
a quarter of a second (docs/ARCHITECTURE.md §10). Some queries are asked on
every single page - the unread-notification badge lives in the shared layout -
and answer the same thing each time. Paying the network for that answer once
per page is waste the user feels directly.

Two rules keep this honest:

* **Opt in, never by default.** A query is cached only when it appears in the
  policy map handed to the cache. Anything a screen polls for freshness, above
  all the analysis-status endpoint the Find My Pet page refreshes while the
  agent works, is deliberately absent.
* **Any write empties it.** `MessageBus` clears the whole cache after every
  command that commits. Partial invalidation would mean naming, for each
  command, every query it could possibly affect - a list that is wrong the
  moment somebody adds a handler and forgets. Clearing everything is slower in
  theory and correct without maintenance.

The time-to-live still matters despite that, because the agent runs in a
separate OS process (CLAUDE.md R2) and its writes cannot clear this cache. A
TTL is the only bound on how long a page can show prose the agent has already
replaced, so it is kept to seconds rather than minutes.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from app.cqrs.base import Query


class QueryCache:
    """Stores query answers for a few seconds, keyed by the query itself.

    Query types are frozen dataclasses, so a query value is hashable and
    compares by its fields: two requests asking the same thing produce equal
    keys without anything having to invent an identifier for them.

    Safe to share between threads. Entries are small and bounded by the number
    of distinct queries asked within one TTL, so nothing evicts on size.
    """

    def __init__(
        self,
        ttl_seconds_by_query_type: Mapping[type[Query], float],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create a cache that stores only the given query types.

        Args:
            ttl_seconds_by_query_type: How long each cacheable query type may
                be reused for. A type absent from this mapping is never
                cached.
            monotonic: Source of the current time, in seconds. Injected so
                expiry can be tested without sleeping. Monotonic rather than
                wall clock, so that a clock adjustment cannot make an entry
                look arbitrarily fresh.
        """
        self._ttl_seconds_by_query_type = dict(ttl_seconds_by_query_type)
        self._monotonic = monotonic
        self._entries: dict[Query, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._hit_count = 0
        self._miss_count = 0

    @property
    def hit_count(self) -> int:
        """How many lookups have been answered from the cache."""
        return self._hit_count

    @property
    def miss_count(self) -> int:
        """How many lookups fell through to the database."""
        return self._miss_count

    def lookup(self, query: Query) -> tuple[bool, Any]:
        """Read a stored answer if one is still fresh.

        Args:
            query: The query being dispatched.

        Returns:
            `(True, answer)` when a fresh answer is stored, `(False, None)`
            otherwise. The flag is separate from the value so that a query
            which legitimately answers None is still served from the cache
            rather than looking like a miss for ever.
        """
        if type(query) not in self._ttl_seconds_by_query_type:
            return False, None

        with self._lock:
            entry = self._entries.get(query)
            if entry is None:
                self._miss_count += 1
                return False, None

            expires_at, answer = entry
            if self._monotonic() >= expires_at:
                del self._entries[query]
                self._miss_count += 1
                return False, None

            self._hit_count += 1
            return True, answer

    def store(self, query: Query, answer: Any) -> None:  # noqa: ANN401 - see lookup
        """Keep an answer for this query's time-to-live.

        Does nothing for a query type that was not opted in, so callers may
        store unconditionally.

        Args:
            query: The query that was answered.
            answer: What the handler returned. Treated as immutable; a caller
                that mutates a returned DTO would change what later requests
                see.
        """
        ttl_seconds = self._ttl_seconds_by_query_type.get(type(query))
        if ttl_seconds is None:
            return

        with self._lock:
            self._entries[query] = (self._monotonic() + ttl_seconds, answer)

    def invalidate_all(self) -> None:
        """Forget every stored answer.

        Called after any command commits. Deliberately total: see the module
        docstring on why per-command invalidation is not worth its failure
        mode.
        """
        with self._lock:
            self._entries.clear()
