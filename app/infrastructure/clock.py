"""The one place "what time is it, for a database column?" is answered.

SQL Server 2014's DATETIME2 columns carry no timezone, and this application
stores UTC in them (see `app.eventstore.store`). Every writer therefore has
to drop the tzinfo at the column boundary - and eight of them had written
their own `datetime.now(UTC).replace(tzinfo=None)`, each as a private
`_now()`, in the command handlers, the worker, two query modules and the
seed scripts.

Eight copies of a one-line rule is eight chances to write one of them
differently: `datetime.utcnow()` looks equivalent, is deprecated, and ruff's
DTZ rules only catch it where they are looking. One function, imported
everywhere, is also the only honest answer to "where does this system decide
what time it is?".

The rule this module encodes, from CLAUDE.md:

    aware in code, naive UTC at the column boundary

so `utc_now()` is for assigning to a column, and `aware_utc_now()` is for
arithmetic - an expiry deadline, an elapsed duration - where mixing an aware
value with a naive one raises.
"""

from __future__ import annotations

from datetime import UTC, datetime


def aware_utc_now() -> datetime:
    """Current UTC time, carrying its timezone.

    For anything computed rather than stored: the 72-hour invitation window
    depends on arithmetic that must not mix aware and naive values.

    Returns:
        The current moment, with tzinfo set to UTC.
    """
    return datetime.now(UTC)


def utc_now() -> datetime:
    """Current UTC time, naive, ready for a DATETIME2 column.

    Returns:
        The current moment in UTC with its timezone label removed, matching
        how every timestamp column in this schema stores one.
    """
    return aware_utc_now().replace(tzinfo=None)


def as_aware_utc(moment: datetime) -> datetime:
    """Read a stored timestamp as UTC, whether or not it carries a timezone.

    The inverse of `as_naive_utc`, and the only correct way to bring a
    DATETIME2 value back into arithmetic: a naive value read from this schema
    is UTC that has merely lost its label, and subtracting it from an aware
    value raises rather than answering wrongly.

    Args:
        moment: Any timestamp, naive or aware.

    Returns:
        The same instant, carrying UTC.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def as_naive_utc(moment: datetime) -> datetime:
    """Strip the timezone off an aware moment, converting it to UTC first.

    Used where a value computed with timezone-aware arithmetic is about to
    be written to a column. Converting before stripping matters: dropping
    tzinfo from a non-UTC moment would store the wrong instant.

    Args:
        moment: Any timestamp, aware or already naive.

    Returns:
        The same instant as naive UTC.
    """
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(UTC).replace(tzinfo=None)
