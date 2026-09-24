"""Tests for asynchronous natural-language search (BG-6, spec 6.3 and 6.4).

`POST /search/describe` used to build an intent interpreter in the request and
call it. On the measured hardware that blocked the worker thread for 11 to 16
seconds (CLAUDE.md R4), on a route open to signed-out visitors, with no length
cap. It now enqueues an `INTERPRET_INTENT` job and redirects.

That makes the interesting tests structural rather than behavioural: the job
exists, it carries the right fields, the page waits, and - the one that
matters - no module under `app/` can reach a language model at all. None of
these tests runs a model, and the completed-job cases store a payload directly
through `intent_to_payload`, which is the contract the agent writes.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from agent_service.intent import SearchIntent, intent_to_payload
from app.domain.enums import AnalysisJobStatus, AnalysisJobType, Species
from app.infrastructure.models import AnalysisJob, new_identifier
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.api

DESCRIPTION = "a small calm rabbit"


def _naive_now() -> datetime:
    """Naive UTC, as the DATETIME columns store."""
    return datetime.now(UTC).replace(tzinfo=None)


def queued_jobs(session_factory: sessionmaker[Session]) -> list[AnalysisJob]:
    """Every interpretation job in the database, oldest first."""
    with session_factory() as session:
        return list(
            session.execute(
                select(AnalysisJob)
                .where(AnalysisJob.job_type == AnalysisJobType.INTERPRET_INTENT.value)
                .order_by(AnalysisJob.created_at)
            )
            .scalars()
            .all()
        )


def add_job(
    session_factory: sessionmaker[Session],
    *,
    status: AnalysisJobStatus,
    described: str = DESCRIPTION,
    adopter_profile_id: str | None = None,
    intent: SearchIntent | None = None,
) -> str:
    """Insert one interpretation job in a given state and return its id.

    The result payload is written through `intent_to_payload`, which is the
    same function the agent uses, so a test cannot accidentally assert
    against a shape the agent does not produce (docs/AGENT.md section 11).
    """
    job_id = new_identifier()
    with session_factory() as session:
        session.add(
            AnalysisJob(
                analysis_job_id=job_id,
                job_type=AnalysisJobType.INTERPRET_INTENT.value,
                status=status.value,
                adopter_profile_id=adopter_profile_id,
                natural_language_query=described,
                result_payload=(
                    json.dumps(intent_to_payload(intent)) if intent is not None else None
                ),
                attempt_count=1,
                created_at=_naive_now(),
            )
        )
        session.commit()
    return job_id


UNDERSTOOD_INTENT = SearchIntent(
    understood=True,
    species=(Species.RABBIT,),
    interpretation="A small, calm rabbit.",
)


class TestQueueingAnInterpretation:
    """A POST enqueues work and redirects; it never interprets anything."""

    def test_a_description_enqueues_exactly_one_job(
        self,
        client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the request hands the work to the agent and stops there.

        One job, not none and not two: this is the whole of BG-6's fix, and
        a route that enqueued twice would double the agent's cost per search.
        """
        response = client.post("/search/describe", data={"description": DESCRIPTION})

        jobs = queued_jobs(session_factory)
        assert len(jobs) == 1
        assert jobs[0].natural_language_query == DESCRIPTION
        assert jobs[0].status == AnalysisJobStatus.PENDING.value
        assert response.status_code == 302
        assert jobs[0].analysis_job_id in response.headers["Location"]

    def test_an_anonymous_search_carries_no_profile(
        self,
        client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves a signed-out visitor's search is bound to nobody.

        There is no profile to fuse, and recording one would attach a
        stranger's search to an adopter.
        """
        client.post("/search/describe", data={"description": DESCRIPTION})

        assert queued_jobs(session_factory)[0].adopter_profile_id is None

    def test_ticking_use_profile_records_the_adopter(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves spec 6.4's fusion is requested at enqueue time.

        The job carries whose profile to combine the intent with, so the
        page that reads it back cannot be talked into using somebody else's.
        """
        adopter_client.post(
            "/search/describe", data={"description": DESCRIPTION, "use_profile": "1"}
        )

        assert (
            queued_jobs(session_factory)[0].adopter_profile_id
            == world["adopter_profile_id"]
        )

    def test_leaving_use_profile_unticked_records_no_profile(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the fusion is opt-in even for a signed-in adopter."""
        adopter_client.post("/search/describe", data={"description": DESCRIPTION})

        assert queued_jobs(session_factory)[0].adopter_profile_id is None


class TestRefusedDescriptions:
    """Negative cases: nothing is queued, and the agent is never troubled."""

    def test_a_blank_description_is_refused(
        self,
        client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves an empty textarea costs the agent nothing.

        400 rather than a redirect to a job that would fail 16 seconds
        later with "needs a natural_language_query".
        """
        response = client.post("/search/describe", data={"description": "   "})

        assert response.status_code == 400
        assert queued_jobs(session_factory) == []

    def test_an_overlong_description_is_refused(
        self,
        client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the server enforces the cap, not the textarea's maxlength.

        A forged post ignores `maxlength` entirely (NFR-5.2), and this text
        becomes a model prompt in another process where its length is
        directly a cost. The route is open to signed-out visitors, so this
        is the one field an unauthenticated caller could use to make the
        agent expensive.
        """
        response = client.post(
            "/search/describe", data={"description": "rabbit " * 200}
        )

        assert response.status_code == 400
        assert queued_jobs(session_factory) == []


class TestWaitingForTheAgent:
    """A queued search shows its waiting state and comes back by itself."""

    def test_a_pending_job_renders_the_pending_block_and_refreshes(
        self,
        client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the page waits visibly and without JavaScript.

        The meta refresh is what makes the result appear on its own; the
        pending block's Refresh link is what makes it reachable if the
        browser ignores the refresh.
        """
        job_id = add_job(session_factory, status=AnalysisJobStatus.PENDING)

        body = client.get(f"/search/describe/{job_id}").get_data(as_text=True)

        assert 'http-equiv="refresh"' in body
        assert "pending-block" in body
        assert DESCRIPTION in body

    def test_a_job_in_progress_still_waits(
        self,
        client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves work already claimed is not mistaken for work finished."""
        job_id = add_job(session_factory, status=AnalysisJobStatus.IN_PROGRESS)

        body = client.get(f"/search/describe/{job_id}").get_data(as_text=True)

        assert 'http-equiv="refresh"' in body

    def test_a_failed_job_offers_the_filters_instead(
        self,
        client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves a dead agent is a dead end for the feature, not for the visitor.

        With Ollama not running - the normal state on a machine only serving
        the web application - this must explain itself and point at the
        structured search, which finds the same animals.
        """
        job_id = add_job(session_factory, status=AnalysisJobStatus.FAILED)

        response = client.get(f"/search/describe/{job_id}")
        body = response.get_data(as_text=True)

        assert response.status_code == 200
        assert 'http-equiv="refresh"' not in body
        assert "/animals/" in body

    def test_an_unknown_job_is_a_404(
        self, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves a guessed identifier reveals nothing and does not error."""
        assert client.get("/search/describe/does-not-exist").status_code == 404


class TestCompletedSearches:
    """A finished interpretation drives the deterministic search."""

    def test_a_completed_job_renders_results(
        self,
        client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the stored intent becomes filters and finds animals.

        Clover is the available rabbit in the shared world, so a rabbit
        intent must reach her. The assertion is on the animal, not on the
        model's prose, because the prose is not deterministic (rule R3).
        """
        job_id = add_job(
            session_factory,
            status=AnalysisJobStatus.COMPLETED,
            intent=UNDERSTOOD_INTENT,
        )

        body = client.get(f"/search/describe/{job_id}").get_data(as_text=True)

        assert "Clover" in body
        assert "A small, calm rabbit." in body

    def test_an_intent_with_no_criteria_explains_itself(
        self,
        client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves an interpretation that extracted nothing is not a silent empty page.

        A negative case for the read side: the job succeeded, and still has
        no criteria to search with.
        """
        job_id = add_job(
            session_factory,
            status=AnalysisJobStatus.COMPLETED,
            intent=SearchIntent(understood=True, interpretation="Nothing specific."),
        )

        body = client.get(f"/search/describe/{job_id}").get_data(as_text=True)

        assert "could not pick out any specific requirements" in body

    def test_a_not_understood_intent_shows_its_reason(
        self,
        client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves an uninterpretable description is an outcome, not an error."""
        job_id = add_job(
            session_factory,
            status=AnalysisJobStatus.COMPLETED,
            intent=SearchIntent.not_understood("We could not make sense of that."),
        )

        body = client.get(f"/search/describe/{job_id}").get_data(as_text=True)

        assert "We could not make sense of that." in body

    def test_a_profile_bound_job_ranks_its_results(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves spec 6.4: the profile ranks what the intent narrowed.

        The score comes from the deterministic scorer, so the page must
        carry one - a fused search that showed the same unranked grid as an
        anonymous one would not be the "key distinction" the spec calls it.
        """
        job_id = add_job(
            session_factory,
            status=AnalysisJobStatus.COMPLETED,
            adopter_profile_id=world["adopter_profile_id"],
            intent=UNDERSTOOD_INTENT,
        )

        body = adopter_client.get(f"/search/describe/{job_id}").get_data(as_text=True)

        assert "Clover" in body
        assert "match with your profile" in body


class TestWhoMaySeeASearch:
    """A search bound to a profile is that adopter's business."""

    def test_another_adopter_is_refused(
        self,
        other_adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves a profile-bound search is not readable by identifier alone.

        What somebody asked for in their own words is personal, and the job
        identifier is the only thing standing between the two adopters.
        """
        job_id = add_job(
            session_factory,
            status=AnalysisJobStatus.COMPLETED,
            adopter_profile_id=world["adopter_profile_id"],
            intent=UNDERSTOOD_INTENT,
        )

        assert other_adopter_client.get(f"/search/describe/{job_id}").status_code == 403

    def test_staff_may_see_any_search(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves staff can follow up a search an adopter mentions to them."""
        job_id = add_job(
            session_factory,
            status=AnalysisJobStatus.COMPLETED,
            adopter_profile_id=world["adopter_profile_id"],
            intent=UNDERSTOOD_INTENT,
        )

        assert staff_client.get(f"/search/describe/{job_id}").status_code == 200

    def test_an_anonymous_search_is_readable_by_anyone_holding_its_id(
        self,
        client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves a search bound to nobody stays usable after the redirect.

        There is nothing personal in it to protect, and requiring sign-in
        would break the signed-out visitor the route exists for.
        """
        job_id = add_job(session_factory, status=AnalysisJobStatus.COMPLETED,
                         intent=UNDERSTOOD_INTENT)

        assert client.get(f"/search/describe/{job_id}").status_code == 200


class TestTheWebTierCannotReachAModel:
    """The structural half of NFR-3.1, checked in the source rather than at runtime."""

    def test_no_module_under_app_imports_the_language_model_client(self) -> None:
        """Proves the web tier cannot make an inference call at all.

        A behavioural test can only show that one route did not call a model
        this time. This shows that no route can: the only way into Ollama is
        the agent package, and nothing under `app/` imports any of it - so a
        future edit that reintroduced a synchronous call would have to add
        the import, and would fail here.

        The shared `INTERPRET_INTENT` contract (docs/AGENT.md section 11)
        lives in `app.domain.search_intent`, which both processes import, so
        the web tier needs nothing from `agent_service` at all (rule R2).
        """
        application_root = Path(__file__).resolve().parents[2] / "app"
        offenders: list[str] = []

        for source_file in application_root.rglob("*.py"):
            tree = ast.parse(source_file.read_text(encoding="utf-8"))
            if _imports_the_model_client(tree):
                offenders.append(str(source_file.relative_to(application_root.parent)))

        assert offenders == [], (
            f"these modules under app/ import the agent process: {offenders}"
        )


def _imports_the_model_client(tree: ast.Module) -> bool:
    """Whether a parsed module imports anything from `agent_service`, anywhere.

    Walks the whole tree rather than looking at top-level statements only:
    the violation this replaces was an import inside a function body, which
    is exactly where somebody would put it back.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and _is_agent_module(node.module):
            return True
        if isinstance(node, ast.Import) and any(
            _is_agent_module(alias.name) for alias in node.names
        ):
            return True
    return False


def _is_agent_module(module_name: str | None) -> bool:
    """Whether a dotted module name is the agent package or one of its modules."""
    return module_name is not None and (
        module_name == "agent_service" or module_name.startswith("agent_service.")
    )
