"""Unit tests for the shared timestamp formatting helper (E-9, docs/UX.md 3).

Four screens formatted dates four different ways with `.strftime` in Jinja,
none of them machine-readable. These prove the one helper that replaced them
gives a page all three forms it needs, and that the two cases most likely to
break it - a naive timestamp out of a SQL Server DATETIME column, and a clock
that is slightly ahead - produce sensible text rather than an exception or a
negative duration.

Pure functions, no database and no application context (rule R2).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from app.cqrs.queries.formatting import (
    describe_relative_time,
    to_display_date,
    to_display_moment,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 23, 14, 5, tzinfo=UTC)


class TestTheThreeForms:
    """Every timestamp is offered as text, as ISO-8601 and as a relative phrase."""

    def test_a_moment_carries_a_date_a_time_and_an_iso_value(self) -> None:
        """Proves a `<time datetime=...>` element can be rendered from one value.

        The ISO form is what makes the date machine-readable - by a screen
        reader, by a browser's own formatting, by anything that reads the
        page - which plain `.strftime` output was not.
        """
        formatted = to_display_moment(NOW - timedelta(hours=2), now=NOW)

        assert formatted.display == "23 Sep 2026, 12:05"
        assert formatted.iso == "2026-09-23T12:05:00+00:00"
        assert formatted.relative_label == "2 hours ago"

    def test_a_date_omits_the_clock(self) -> None:
        """Proves the date style drops a time of day nobody reads.

        A submission date is a day, not a minute; showing 14:05 beside it
        invites a precision the value does not carry.
        """
        formatted = to_display_date(NOW, now=NOW)

        assert formatted.display == "23 Sep 2026"
        assert ":" not in formatted.display

    def test_a_naive_timestamp_is_read_as_utc(self) -> None:
        """Proves a value straight out of a DATETIME column formats correctly.

        SQL Server 2014 has no timezone-aware type, so every timestamp this
        application reads back is naive UTC. Mixing one with an aware value
        raises in Python, which would have been a 500 on four screens.
        """
        formatted = to_display_moment(
            datetime(2026, 9, 23, 13, 5), now=NOW  # noqa: DTZ001 - the point of the test
        )

        assert formatted.display == "23 Sep 2026, 13:05"
        assert formatted.relative_label == "1 hour ago"

    def test_a_timestamp_in_another_zone_is_converted(self) -> None:
        """Proves an aware value is normalised rather than displayed as written."""
        three_hours_ahead = timezone(timedelta(hours=3))
        formatted = to_display_moment(
            datetime(2026, 9, 23, 15, 5, tzinfo=three_hours_ahead), now=NOW
        )

        assert formatted.display == "23 Sep 2026, 12:05"


class TestRelativeLabels:
    """The phrasing a pending block and an activity feed read out."""

    @pytest.mark.parametrize(
        ("elapsed", "expected"),
        [
            (timedelta(seconds=5), "just now"),
            (timedelta(seconds=59), "just now"),
            (timedelta(minutes=1), "1 minute ago"),
            (timedelta(minutes=4), "4 minutes ago"),
            (timedelta(hours=1), "1 hour ago"),
            (timedelta(hours=23), "23 hours ago"),
            (timedelta(days=1), "1 day ago"),
            (timedelta(days=7), "7 days ago"),
        ],
    )
    def test_each_band_is_phrased_with_a_real_plural(
        self, elapsed: timedelta, expected: str
    ) -> None:
        """Proves the labels pluralise properly and never read "1 minutes ago".

        The boundaries are included deliberately: 59 seconds and 60 seconds
        are the two sides of the first band, and an off-by-one there shows
        on every pending block on the site.
        """
        assert describe_relative_time(NOW - elapsed, now=NOW) == expected

    def test_something_older_than_a_week_is_given_as_a_date(self) -> None:
        """Proves a long-past event is dated rather than counted.

        "43 days ago" is arithmetic the reader has to undo; the date is the
        thing they actually want by then.
        """
        assert describe_relative_time(NOW - timedelta(days=43), now=NOW) == "on 11 Aug 2026"

    def test_a_future_timestamp_does_not_read_as_a_negative_duration(self) -> None:
        """Proves clock skew degrades to "just now" rather than to nonsense.

        The application server and the shared cloud database do not agree to
        the second, so a row written moments ago can carry a timestamp
        slightly in the future. "in -3 minutes" would be worse than vague.
        """
        assert describe_relative_time(NOW + timedelta(minutes=3), now=NOW) == "just now"
