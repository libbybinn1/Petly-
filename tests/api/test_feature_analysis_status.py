"""Tests for the analysis-status JSON endpoint (NFR-3.1).

The endpoint exists so a page can poll without re-rendering, because the
agent writes explanations in a separate process and blocking the request
until it finishes is what rule R4 forbids.

The interesting tests are the authorization ones. A polling endpoint is
easy to leave open by accident: it looks like plumbing rather than data,
and it is the kind of URL nobody links to and so nobody checks.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.domain.enums import AnalysisJobStatus, AnalysisJobType, MatchDirection
from app.infrastructure.models import AnalysisJob, MatchAnalysis, new_identifier
from flask.testing import FlaskClient
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.api


def _naive_now() -> datetime:
    """Naive UTC, as the DATETIME2 columns store."""
    return datetime.now(UTC).replace(tzinfo=None)


def add_job(
    session_factory: sessionmaker[Session],
    *,
    adopter_profile_id: str | None = None,
    animal_id: str | None = None,
    status: AnalysisJobStatus = AnalysisJobStatus.PENDING,
    minutes_ago: int = 1,
) -> str:
    """Queue one analysis job and return its identifier."""
    job_id = new_identifier()
    with session_factory() as session:
        session.add(
            AnalysisJob(
                analysis_job_id=job_id,
                job_type=AnalysisJobType.FIND_MY_PET.value,
                status=status.value,
                adopter_profile_id=adopter_profile_id,
                animal_id=animal_id,
                attempt_count=0,
                created_at=_naive_now() - timedelta(minutes=minutes_ago),
            )
        )
        session.commit()
    return job_id


def add_analysis(
    session_factory: sessionmaker[Session],
    adopter_profile_id: str,
    animal_id: str,
    *,
    minutes_ago: int = 1,
) -> None:
    """Store one completed analysis."""
    with session_factory() as session:
        session.add(
            MatchAnalysis(
                match_analysis_id=new_identifier(),
                direction=MatchDirection.ADOPTER_TO_ANIMAL.value,
                adopter_profile_id=adopter_profile_id,
                animal_id=animal_id,
                score=80,
                is_disqualified=False,
                model_name="stub",
                generated_at=_naive_now() - timedelta(minutes=minutes_ago),
            )
        )
        session.commit()


class TestAuthorization:
    """A polling endpoint is data, not plumbing."""

    def test_an_anonymous_visitor_cannot_poll(self, client: FlaskClient) -> None:
        """Proves the endpoint is not open."""
        response = client.get("/api/analysis-status?scope=my-matches")

        assert response.status_code in (302, 401)

    def test_an_adopter_cannot_ask_about_an_animal(
        self, adopter_client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves the staff scope is staff-only.

        An adopter could otherwise watch how much attention one animal is
        getting, which is not theirs to see.
        """
        response = adopter_client.get(
            f"/api/analysis-status?animal_id={world['available_animal_id']}"
        )

        assert response.status_code == 403

    def test_an_adopter_can_only_ask_about_their_own_matches(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the adopter scope takes no identifier from the caller.

        The profile identifier comes from the session, so asking about
        somebody else is not expressible rather than merely refused.
        """
        add_job(session_factory, adopter_profile_id=world["other_profile_id"])

        payload = adopter_client.get(
            "/api/analysis-status?scope=my-matches"
        ).get_json()

        assert payload["pending"] == 0

    def test_a_request_with_no_scope_is_refused(
        self, adopter_client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves an unscoped poll does not report the whole queue."""
        assert adopter_client.get("/api/analysis-status").status_code == 400


class TestTheCounts:
    """The numbers describe the scope that was asked about."""

    def test_a_pending_job_is_counted(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves outstanding work is visible to the page."""
        add_job(session_factory, adopter_profile_id=world["adopter_profile_id"])

        payload = adopter_client.get(
            "/api/analysis-status?scope=my-matches"
        ).get_json()

        assert payload["pending"] == 1
        assert payload["oldest_pending_at"] is not None

    def test_an_in_progress_job_still_counts_as_pending(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the page waits on work already started.

        Splitting PENDING from IN_PROGRESS here would make a page stop
        polling while the agent was mid-analysis.
        """
        add_job(
            session_factory,
            adopter_profile_id=world["adopter_profile_id"],
            status=AnalysisJobStatus.IN_PROGRESS,
        )

        payload = adopter_client.get(
            "/api/analysis-status?scope=my-matches"
        ).get_json()

        assert payload["pending"] == 1

    def test_completed_and_failed_are_reported_separately(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves a failure is distinguishable from a success.

        A page that treated them alike would wait for ever on a job that
        already gave up.
        """
        add_job(
            session_factory,
            adopter_profile_id=world["adopter_profile_id"],
            status=AnalysisJobStatus.COMPLETED,
        )
        add_job(
            session_factory,
            adopter_profile_id=world["adopter_profile_id"],
            status=AnalysisJobStatus.FAILED,
        )

        payload = adopter_client.get(
            "/api/analysis-status?scope=my-matches"
        ).get_json()

        assert payload["pending"] == 0
        assert payload["completed"] == 1
        assert payload["failed"] == 1

    def test_an_empty_scope_answers_zeroes_rather_than_failing(
        self, adopter_client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves nothing queued is a valid answer, not an error."""
        payload = adopter_client.get(
            "/api/analysis-status?scope=my-matches"
        ).get_json()

        assert payload == {
            "pending": 0,
            "completed": 0,
            "failed": 0,
            "generation": 0,
            "oldest_pending_at": None,
        }


class TestTheGeneration:
    """`generation` is what tells the page something actually changed."""

    def test_it_is_zero_with_no_analyses(
        self, adopter_client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves the absence of analyses is not an error either."""
        payload = adopter_client.get(
            "/api/analysis-status?scope=my-matches"
        ).get_json()

        assert payload["generation"] == 0

    def test_it_changes_when_a_newer_analysis_arrives(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the page can detect new work by comparing two answers.

        This is the whole contract: the caller needs one integer it can
        compare with `!=`, rather than having to parse and order dates.
        """
        add_analysis(
            session_factory,
            world["adopter_profile_id"],
            world["available_animal_id"],
            minutes_ago=60,
        )
        before = adopter_client.get(
            "/api/analysis-status?scope=my-matches"
        ).get_json()["generation"]

        add_analysis(
            session_factory,
            world["adopter_profile_id"],
            world["adopted_animal_id"],
            minutes_ago=0,
        )
        after = adopter_client.get(
            "/api/analysis-status?scope=my-matches"
        ).get_json()["generation"]

        assert after > before

    def test_staff_can_watch_one_animal(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the staff scope works and stays narrow."""
        add_job(
            session_factory,
            animal_id=world["available_animal_id"],
            status=AnalysisJobStatus.PENDING,
        )
        add_job(
            session_factory,
            animal_id=world["adopted_animal_id"],
            status=AnalysisJobStatus.PENDING,
        )

        payload = staff_client.get(
            f"/api/analysis-status?animal_id={world['available_animal_id']}"
        ).get_json()

        assert payload["pending"] == 1

    def test_the_response_names_no_records(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves polling cannot be used to enumerate anything.

        The endpoint is called repeatedly and by anyone signed in, so it
        should carry counts and a timestamp and nothing else.
        """
        add_analysis(
            session_factory,
            world["adopter_profile_id"],
            world["available_animal_id"],
        )

        payload = adopter_client.get(
            "/api/analysis-status?scope=my-matches"
        ).get_json()

        assert set(payload) == {
            "pending",
            "completed",
            "failed",
            "generation",
            "oldest_pending_at",
        }


class TestTimestampsAreReadAsUtc:
    """The DATETIME2 columns are naive, and both of these read them wrongly.

    A naive value is UTC that lost its label (app.eventstore.store), so
    `.timestamp()` and `.isoformat()` on one answered as if it were local
    time: the generation number was out by the machine's UTC offset, and the
    ISO string carried no offset for the browser to correct by.
    """

    def test_the_oldest_pending_timestamp_carries_an_offset(self) -> None:
        """Proves the polled payload states the timezone it means."""
        from app.cqrs.queries.analysis_status_queries import AnalysisStatus

        status = AnalysisStatus(
            pending=1,
            completed=0,
            failed=0,
            generation=0,
            # Naive on purpose: this is exactly what the column hands back,
            # and the point of the fix is that it is read as UTC.
            oldest_pending_at=datetime(2026, 9, 24, 8, 30, tzinfo=UTC).replace(tzinfo=None),
        )

        assert status.as_dictionary()["oldest_pending_at"] == "2026-09-24T08:30:00+00:00"

    def test_an_absent_timestamp_stays_null(self) -> None:
        """Proves a settled queue answers null rather than a converted epoch."""
        from app.cqrs.queries.analysis_status_queries import AnalysisStatus

        status = AnalysisStatus(
            pending=0, completed=3, failed=0, generation=1, oldest_pending_at=None
        )

        assert status.as_dictionary()["oldest_pending_at"] is None
