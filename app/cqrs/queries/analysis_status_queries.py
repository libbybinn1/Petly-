"""Read-side query for how far the agent has got (NFR-3.1).

The agent runs as a separate process and writes explanations
asynchronously, so a page showing them is showing a moving target. The
alternative - waiting for the agent inside the request - is what rule R4
forbids, because CPU-only inference was measured at 11 to 16 seconds a
call.

This lets a page ask, cheaply and repeatedly, whether anything has
changed. It counts rows and reads two timestamps; it never touches a
model.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.cqrs.base import Query, QueryHandler
from app.domain.enums import AnalysisJobStatus
from app.infrastructure.models import AnalysisJob, MatchAnalysis

SelectT = TypeVar("SelectT", bound=Select[Any])

# The statuses that mean "still coming". IN_PROGRESS counts as pending
# because the page is waiting on it just the same.
OUTSTANDING_STATUSES = (
    AnalysisJobStatus.PENDING.value,
    AnalysisJobStatus.IN_PROGRESS.value,
)


@dataclass(frozen=True)
class AnalysisStatus:
    """How much agent work is outstanding for one scope."""

    pending: int
    completed: int
    failed: int
    generation: int
    oldest_pending_at: datetime | None

    @property
    def is_settled(self) -> bool:
        """Whether there is nothing left to wait for."""
        return self.pending == 0

    def as_dictionary(self) -> dict[str, Any]:
        """Shape this for the JSON endpoint.

        `generation` is the newest analysis timestamp in whole seconds, so
        a caller can compare two answers with `!=` without parsing dates.
        Nothing here identifies a record, so polling it leaks nothing
        about anybody's data.
        """
        return {
            "pending": self.pending,
            "completed": self.completed,
            "failed": self.failed,
            "generation": self.generation,
            "oldest_pending_at": (
                self.oldest_pending_at.isoformat()
                if self.oldest_pending_at is not None
                else None
            ),
        }


@dataclass(frozen=True)
class GetAnalysisStatusQuery(Query):
    """Ask how much agent work is outstanding.

    One scope is meaningful at a time: an adopter asking about their own
    matches, or a staff member asking about one animal.
    """

    adopter_profile_id: str | None = None
    animal_id: str | None = None

    @property
    def has_scope(self) -> bool:
        """Whether the caller narrowed the question to anything."""
        return self.adopter_profile_id is not None or self.animal_id is not None


class GetAnalysisStatusHandler(QueryHandler[AnalysisStatus]):
    """Answers GetAnalysisStatusQuery."""

    def handle(self, query: Query, session: Session) -> AnalysisStatus:
        """Count the jobs in scope and find the newest analysis.

        An unscoped query answers zeroes rather than counting the whole
        queue. A caller with no scope has asked about nothing in
        particular, and reporting the system-wide backlog to them would be
        both meaningless and a small leak.
        """
        assert isinstance(query, GetAnalysisStatusQuery)

        if not query.has_scope:
            return AnalysisStatus(
                pending=0, completed=0, failed=0, generation=0, oldest_pending_at=None
            )

        counts = _count_jobs_by_status(session, query)
        outstanding = sum(counts.get(status, 0) for status in OUTSTANDING_STATUSES)

        return AnalysisStatus(
            pending=outstanding,
            completed=counts.get(AnalysisJobStatus.COMPLETED.value, 0),
            failed=counts.get(AnalysisJobStatus.FAILED.value, 0),
            generation=_newest_analysis_epoch(session, query),
            oldest_pending_at=_oldest_outstanding_at(session, query),
        )


def _narrow_to_scope(statement: SelectT, query: GetAnalysisStatusQuery) -> SelectT:
    """Restrict a job query to the adopter or animal that was asked about.

    Generic over the statement so the same narrowing serves both the
    grouped count and the minimum-timestamp query, which select different
    things and must each keep their own row type.
    """
    if query.adopter_profile_id is not None:
        statement = statement.where(
            AnalysisJob.adopter_profile_id == query.adopter_profile_id
        )
    if query.animal_id is not None:
        statement = statement.where(AnalysisJob.animal_id == query.animal_id)
    return statement


def _count_jobs_by_status(
    session: Session, query: GetAnalysisStatusQuery
) -> dict[str, int]:
    """Count this scope's jobs, grouped by status, in one query."""
    statement = _narrow_to_scope(
        select(AnalysisJob.status, func.count()).group_by(AnalysisJob.status), query
    )
    return dict(session.execute(statement).tuples().all())


def _newest_analysis_epoch(session: Session, query: GetAnalysisStatusQuery) -> int:
    """Whole seconds of the newest analysis in scope, or 0 if there is none."""
    statement = select(func.max(MatchAnalysis.generated_at))
    if query.adopter_profile_id is not None:
        statement = statement.where(
            MatchAnalysis.adopter_profile_id == query.adopter_profile_id
        )
    if query.animal_id is not None:
        statement = statement.where(MatchAnalysis.animal_id == query.animal_id)

    newest: datetime | None = session.execute(statement).scalar_one_or_none()
    if newest is None:
        return 0
    return int(newest.timestamp())


def _oldest_outstanding_at(
    session: Session, query: GetAnalysisStatusQuery
) -> datetime | None:
    """When the longest-waiting outstanding job was queued.

    Lets the page say "this has been going a while" rather than spinning
    silently, and lets staff see a stuck queue.
    """
    statement = _narrow_to_scope(
        select(func.min(AnalysisJob.created_at)).where(
            AnalysisJob.status.in_(OUTSTANDING_STATUSES)
        ),
        query,
    )
    oldest: datetime | None = session.execute(statement).scalar_one_or_none()
    return oldest
