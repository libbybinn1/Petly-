"""Shared timestamp formatting for read models (docs/UX.md section 3).

A template displays; it does not decide (rule R2). A `.strftime` call in Jinja
is a small decision - which format, which timezone, what to do when the value
is absent - repeated once per screen and drifting a little every time. Four
screens had four formats, none of them machine-readable, so a date could not
be read by anything but a human eye.

Every timestamp a view model exposes is therefore formatted here, once, into
the three forms a page actually needs:

    display          what a person reads: "23 Sep 2026, 14:05"
    iso              the machine-readable value for `<time datetime="...">`
    relative_label   how long ago it was: "4 minutes ago"

A template then writes the same three tokens everywhere:

    <time datetime="{{ x.iso }}">{{ x.display }}</time>

Two styles rather than one function with a boolean: a submission date wants
no clock, an event on a timeline does, and `to_display_moment(moment, True)`
would say nothing about which is which at the call site.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

DATE_PATTERN = "%d %b %Y"
MOMENT_PATTERN = "%d %b %Y, %H:%M"

SECONDS_PER_MINUTE = 60
MINUTES_PER_HOUR = 60
HOURS_PER_DAY = 24

# Past a week, "9 days ago" is less useful than the date itself.
RELATIVE_LABEL_DAY_LIMIT = 7


@dataclass(frozen=True)
class DisplayDate:
    """One timestamp in the three forms a screen needs.

    Attributes:
        display: Human-readable text.
        iso: ISO-8601, for the `datetime` attribute of a `<time>` element.
        relative_label: How long ago, in prose.
    """

    display: str
    iso: str
    relative_label: str


def to_display_date(moment: datetime, now: datetime | None = None) -> DisplayDate:
    """Format a timestamp as a calendar date, with no clock time.

    For values where the time of day is noise: the day an application was
    submitted, not the minute (docs/UX.md section 3).

    Args:
        moment: The timestamp. A naive value is read as UTC, which is how
            every DATETIME column in this schema stores one.
        now: Override for the comparison point, so a test is deterministic.

    Returns:
        The date in all three display forms.
    """
    return _build(moment, DATE_PATTERN, now)


def to_display_moment(moment: datetime, now: datetime | None = None) -> DisplayDate:
    """Format a timestamp as a date and a clock time.

    For values where the minute matters: an entry on an activity feed or a
    history timeline.

    Args:
        moment: The timestamp. A naive value is read as UTC.
        now: Override for the comparison point, so a test is deterministic.

    Returns:
        The moment in all three display forms.
    """
    return _build(moment, MOMENT_PATTERN, now)


def describe_relative_time(moment: datetime, now: datetime | None = None) -> str:
    """Say how long ago something happened, in prose.

    Used on its own for the "Queued 4 minutes ago" line under a pending
    analysis, where the exact minute is not the point and the wait is
    (CLAUDE.md R4).

    A timestamp in the future reads as "just now" rather than as a negative
    duration: clock skew between this machine and a shared cloud database is
    real, and "in -3 minutes" would be a worse answer than a vague one.

    Args:
        moment: The timestamp. A naive value is read as UTC.
        now: Override for the comparison point, so a test is deterministic.

    Returns:
        A phrase such as "just now", "4 minutes ago" or "on 23 Sep 2026".
    """
    aware_moment = _as_utc(moment)
    comparison_point = _as_utc(now) if now is not None else datetime.now(UTC)
    elapsed_seconds = (comparison_point - aware_moment).total_seconds()

    if elapsed_seconds < SECONDS_PER_MINUTE:
        return "just now"

    minutes = int(elapsed_seconds // SECONDS_PER_MINUTE)
    if minutes < MINUTES_PER_HOUR:
        return _count_of(minutes, "minute")

    hours = minutes // MINUTES_PER_HOUR
    if hours < HOURS_PER_DAY:
        return _count_of(hours, "hour")

    days = hours // HOURS_PER_DAY
    if days <= RELATIVE_LABEL_DAY_LIMIT:
        return _count_of(days, "day")

    return f"on {aware_moment.strftime(DATE_PATTERN)}"


def _build(moment: datetime, pattern: str, now: datetime | None) -> DisplayDate:
    """Assemble the three display forms of one timestamp."""
    aware_moment = _as_utc(moment)
    return DisplayDate(
        display=aware_moment.strftime(pattern),
        iso=aware_moment.isoformat(),
        relative_label=describe_relative_time(aware_moment, now),
    )


def _as_utc(moment: datetime) -> datetime:
    """Read a timestamp as UTC, whether or not it carries a timezone.

    SQL Server 2014 DATETIME columns are naive and this application stores
    UTC in them (see `app.eventstore.store`), so a naive value read back is
    UTC that has merely lost the label. Attaching it here means arithmetic
    below never mixes an aware value with a naive one, which raises.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _count_of(quantity: int, noun: str) -> str:
    """Phrase a count with a correctly pluralised noun and no "(s)"."""
    return f"{quantity} {noun}{'' if quantity == 1 else 's'} ago"
