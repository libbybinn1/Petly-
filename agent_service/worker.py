"""The independent agent process: claims jobs and records analyses.

This is the process boundary that makes the agent genuinely separate
(blueprint requirement 4.5). It polls the `analysis_jobs` table, runs the
agent loop, writes a `MatchAnalysis` and appends `AIAnalysisCompleted`.

What it imports is worth being precise about, because docs used to overstate
it: the worker imports the ORM models and the configuration module, because
the queue *is* a database table and it has to be read. It imports no Flask
application, no controller, no command and no command bus - so there is no
code path by which the agent could approve, reject or alter an application
(rule R4). That is the boundary that matters.

Run it with the interpreter outside the project - README.md explains why a
virtual environment inside OneDrive corrupts itself:

    <venv>/Scripts/python.exe -m agent_service
"""

from __future__ import annotations

import json
import logging
import signal
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import FrameType
from typing import Any, Protocol

from app.config import Configuration, load_configuration
from app.domain.enums import (
    AggregateType,
    AnalysisJobStatus,
    AnalysisJobType,
    DomainEventType,
    MatchDirection,
)
from app.eventstore.store import EventStore
from app.infrastructure.database import create_database_engine, create_session_factory
from app.infrastructure.models import (
    AdopterProfile,
    AnalysisJob,
    Animal,
    MatchAnalysis,
    new_identifier,
)
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from agent_service.explanation import DETERMINISTIC_FALLBACK_MODEL
from agent_service.intent import IntentInterpreter, intent_to_payload
from agent_service.llm_client import LanguageModel, OllamaLanguageModel
from agent_service.loop import AnalysisOutcome, MatchAnalysisAgent, MissingRecordError
from agent_service.rag.knowledge_base import EmbeddingClient, KnowledgeBase
from agent_service.tools.mcp_tools import McpToolClient
from agent_service.tools.web_search import build_search_provider

logger = logging.getLogger("petmatch.agent")

# A job that keeps failing is parked rather than retried forever, so one bad
# record cannot monopolise the worker.
MAX_JOB_ATTEMPTS = 3

# A worker killed between claiming a job and persisting its result leaves the
# row IN_PROGRESS with nobody working on it. Nothing would ever pick it up
# again, so the adopter's explanation would never appear and the dashboard
# would never report it as failed. Anything claimed longer ago than this is
# assumed abandoned: comfortably longer than a real analysis, which is bound
# by the step cap at roughly a minute of CPU inference.
STRANDED_JOB_TIMEOUT_MINUTES = 15

DIRECTION_BY_JOB_TYPE = {
    AnalysisJobType.RANK_APPLICANT: MatchDirection.ANIMAL_TO_ADOPTER,
    AnalysisJobType.FIND_MORE_ADOPTERS: MatchDirection.ANIMAL_TO_ADOPTER,
    AnalysisJobType.FIND_MY_PET: MatchDirection.ADOPTER_TO_ANIMAL,
}


class MatchAnalyser(Protocol):
    """The analysis capability the worker depends on.

    Declared as a protocol so the worker depends on *one method* rather than
    on the concrete agent, its language model, its vector store and its
    subprocess. A queue test can then drive the worker with a double that
    returns a fixed outcome, which is the only way to test the queue's own
    behaviour deterministically (rule R3).
    """

    def analyse(
        self, adopter_profile_id: str, animal_id: str, direction: MatchDirection
    ) -> AnalysisOutcome:
        """Assess one adopter against one animal."""
        ...


@dataclass
class WorkerStatistics:
    """Counters reported when the worker stops."""

    completed: int = 0
    failed: int = 0
    skipped: int = 0
    reused: int = 0


class AgentWorker:
    """Polls the job queue and runs analyses until asked to stop."""

    def __init__(
        self,
        configuration: Configuration,
        session_factory: sessionmaker[Session],
        agent: MatchAnalyser,
        language_model: LanguageModel | None = None,
    ) -> None:
        """Wire the worker to its queue, its agent and its model.

        Args:
            configuration: Poll interval and agent settings.
            session_factory: How to open a session onto the queue.
            agent: What runs one analysis.
            language_model: Used for `INTERPRET_INTENT` jobs, which need a
                model but no scoring. Optional so a queue test needs no
                model at all; such a job is failed with a clear message
                rather than silently mis-answered when none is configured.
        """
        self._configuration = configuration
        self._session_factory = session_factory
        self._agent = agent
        self._language_model = language_model
        self._should_stop = False
        self.statistics = WorkerStatistics()

    def request_stop(self) -> None:
        """Ask the loop to finish the current job and exit."""
        self._should_stop = True

    def run_forever(self) -> WorkerStatistics:
        """Poll until asked to stop.

        Returns:
            The counters accumulated over the run.
        """
        interval = self._configuration.agent.poll_interval_seconds
        logger.info("agent worker started, polling every %ss", interval)

        while not self._should_stop:
            if not self.run_once():
                time.sleep(interval)

        logger.info(
            "agent worker stopped: %s completed, %s reused, %s failed, %s skipped",
            self.statistics.completed,
            self.statistics.reused,
            self.statistics.failed,
            self.statistics.skipped,
        )
        return self.statistics

    def run_once(self) -> bool:
        """Claim and process at most one job.

        Returns:
            True when a job was processed, False when the queue was empty.
        """
        self._reclaim_stranded_jobs()

        job = self._claim_next_job()
        if job is None:
            return False

        logger.info("claimed job %s (%s)", job.analysis_job_id, job.job_type)
        try:
            self._process(job)
        except MissingRecordError as error:
            self._finish_failed(job.analysis_job_id, f"missing record: {error}")
        except Exception as error:  # - one bad job must not kill the worker
            logger.exception("job %s failed", job.analysis_job_id)
            self._finish_failed(job.analysis_job_id, f"{type(error).__name__}: {error}")
        return True

    # ------------------------------------------------------------------
    # The queue
    # ------------------------------------------------------------------

    def _claim_next_job(self) -> AnalysisJob | None:
        """Take the oldest pending job and mark it in progress.

        The claim is a single conditional UPDATE whose row count decides the
        winner. A SELECT followed by an unconditional UPDATE is not enough:
        two workers whose selects interleave would both believe they had
        claimed the row, and both would write an analysis and an event for
        it. `WHERE status = 'PENDING'` makes the second update match nothing,
        which is portable to both SQL Server 2014 and the SQLite used in
        tests.

        Returns:
            The claimed job, detached from the session, or None when there
            was nothing to claim - including when another worker won the
            race.
        """
        with self._session_factory() as session:
            candidate_id = session.execute(
                select(AnalysisJob.analysis_job_id)
                .where(AnalysisJob.status == AnalysisJobStatus.PENDING.value)
                .order_by(AnalysisJob.created_at)
                .limit(1)
            ).scalar_one_or_none()

            if candidate_id is None:
                return None

            claimed_at = _now()
            claimed = session.execute(
                update(AnalysisJob)
                .where(
                    AnalysisJob.analysis_job_id == candidate_id,
                    AnalysisJob.status == AnalysisJobStatus.PENDING.value,
                )
                .values(
                    status=AnalysisJobStatus.IN_PROGRESS.value,
                    started_at=claimed_at,
                    attempt_count=AnalysisJob.attempt_count + 1,
                )
            )
            if claimed.rowcount != 1:
                session.rollback()
                logger.info("job %s was claimed by another worker", candidate_id)
                return None

            session.commit()

            job = session.get(AnalysisJob, candidate_id)
            if job is None:
                return None
            session.expunge(job)
            return job

    def _reclaim_stranded_jobs(self) -> None:
        """Return jobs abandoned mid-flight to the queue, or park them.

        A worker that dies between claiming and persisting leaves its row
        IN_PROGRESS forever, because the claim only ever looks at PENDING
        rows. Sweeping on each poll costs one indexed query and is what makes
        a crash recoverable without an operator.
        """
        cutoff = _now() - timedelta(minutes=STRANDED_JOB_TIMEOUT_MINUTES)

        with self._session_factory() as session:
            stranded = (
                session.execute(
                    select(AnalysisJob).where(
                        AnalysisJob.status == AnalysisJobStatus.IN_PROGRESS.value,
                        or_(
                            AnalysisJob.started_at.is_(None),
                            AnalysisJob.started_at < cutoff,
                        ),
                    )
                )
                .scalars()
                .all()
            )
            for job in stranded:
                self._reset_stranded(job)
            if stranded:
                session.commit()

    def _reset_stranded(self, job: AnalysisJob) -> None:
        """Send one abandoned job back to PENDING, or fail it for good."""
        job.error_message = (
            f"reclaimed: still IN_PROGRESS more than "
            f"{STRANDED_JOB_TIMEOUT_MINUTES} minutes after it was claimed"
        )
        if job.attempt_count >= MAX_JOB_ATTEMPTS:
            job.status = AnalysisJobStatus.FAILED.value
            job.completed_at = _now()
            self.statistics.failed += 1
            logger.error("job %s stranded and out of attempts; failed", job.analysis_job_id)
            return

        job.status = AnalysisJobStatus.PENDING.value
        logger.warning(
            "job %s was stranded in progress; returned to the queue", job.analysis_job_id
        )

    # ------------------------------------------------------------------
    # Processing one job
    # ------------------------------------------------------------------

    def _process(self, job: AnalysisJob) -> None:
        """Run the work one claimed job asks for.

        Args:
            job: The claimed job.
        """
        job_type = AnalysisJobType(job.job_type)
        if job_type is AnalysisJobType.INTERPRET_INTENT:
            self._interpret_intent(job)
            return

        direction = DIRECTION_BY_JOB_TYPE.get(job_type)
        if direction is None or not job.adopter_profile_id or not job.animal_id:
            self._skip(job, f"job type {job.job_type} needs both an adopter and an animal")
            return

        if self._reuse_cached_analysis(job, direction):
            return

        outcome = self._agent.analyse(job.adopter_profile_id, job.animal_id, direction)
        self._persist(job, outcome, direction)
        self.statistics.completed += 1
        logger.info(
            "job %s complete: score=%s model=%s steps=%s",
            job.analysis_job_id,
            outcome.score.score,
            outcome.model_name,
            len(outcome.reasoning_trace),
        )

    def _interpret_intent(self, job: AnalysisJob) -> None:
        """Turn an adopter's own words into stored search criteria (spec 6.3).

        This job type produces no analysis and no domain event: it answers a
        question the web tier asked, so the answer goes back on the job row
        as JSON for the web tier to read.

        Args:
            job: The claimed `INTERPRET_INTENT` job.
        """
        free_text = (job.natural_language_query or "").strip()
        if not free_text:
            self._skip(job, "an INTERPRET_INTENT job needs a natural_language_query")
            return

        if self._language_model is None:
            self._skip(job, "no language model is configured for intent interpretation")
            return

        intent = IntentInterpreter(self._language_model).interpret(free_text)
        self._store_result(job.analysis_job_id, intent_to_payload(intent))
        self.statistics.completed += 1
        logger.info(
            "job %s interpreted intent: understood=%s", job.analysis_job_id, intent.understood
        )

    def _skip(self, job: AnalysisJob, reason: str) -> None:
        """Record a job the worker cannot run, with why."""
        self._finish_failed(job.analysis_job_id, reason)
        self.statistics.skipped += 1

    def _reuse_cached_analysis(self, job: AnalysisJob, direction: MatchDirection) -> bool:
        """Complete the job from a stored analysis when nothing has changed.

        NFR-3.3: a completed analysis is cached and not recomputed for
        unchanged inputs. On CPU-only inference an explanation costs roughly
        16 seconds, so re-deriving one for records that have not moved is the
        most expensive thing the worker could do for no benefit.

        Freshness is decided by the records themselves: an analysis generated
        after both `updated_at` timestamps still describes the current facts.
        A deterministic-fallback row is never reused - see `_is_reusable`.

        Args:
            job: The claimed job.
            direction: Which weighting this job asks for.

        Returns:
            True when the cached analysis was reused and the job closed.
        """
        with self._session_factory() as session:
            existing = _find_existing_analysis(session, job, direction)
            if existing is None or not _is_reusable(session, existing):
                return False

            analysis_id = existing.match_analysis_id
            _close_job(session, job.analysis_job_id)
            session.commit()

        self.statistics.reused += 1
        logger.info("job %s reused cached analysis %s", job.analysis_job_id, analysis_id)
        return True

    def _persist(
        self, job: AnalysisJob, outcome: AnalysisOutcome, direction: MatchDirection
    ) -> None:
        """Write the analysis, append its event and close the job.

        All three happen in one transaction: an analysis without its event,
        or a job marked complete with nothing stored, would both be worse
        than a clean retry.

        An analysis for the same pairing is updated in place rather than
        inserted again, so a recomputation replaces the stale row instead of
        accumulating duplicates the read models would silently hide.

        Args:
            job: The claimed job.
            outcome: What the analysis produced.
            direction: Which weighting was applied.
        """
        with self._session_factory() as session:
            existing = _find_existing_analysis(session, job, direction)
            analysis_id = (
                existing.match_analysis_id if existing is not None else new_identifier()
            )
            columns = _analysis_columns(outcome)

            if existing is None:
                session.add(
                    MatchAnalysis(
                        match_analysis_id=analysis_id,
                        direction=direction.value,
                        adopter_profile_id=job.adopter_profile_id,
                        animal_id=job.animal_id,
                        application_id=job.application_id,
                        **columns,
                    )
                )
            else:
                for column_name, value in columns.items():
                    setattr(existing, column_name, value)

            _append_analysis_event(session, job, outcome, direction, analysis_id)
            _close_job(session, job.analysis_job_id)
            session.commit()

    def _store_result(self, job_id: str, payload: dict[str, Any]) -> None:
        """Store a job's JSON result on its own row and complete it.

        Args:
            job_id: Which job answered.
            payload: The result, serialised into the NVARCHAR(MAX) column
                because SQL Server 2014 has no JSON type.
        """
        with self._session_factory() as session:
            job = session.get(AnalysisJob, job_id)
            if job is None:
                return
            job.result_payload = json.dumps(payload)
            job.status = AnalysisJobStatus.COMPLETED.value
            job.completed_at = _now()
            session.commit()

    def _finish_failed(self, job_id: str, message: str) -> None:
        """Record a failure, returning the job to the queue if attempts remain."""
        with self._session_factory() as session:
            job = session.get(AnalysisJob, job_id)
            if job is None:
                return

            job.error_message = message[:1000]
            if job.attempt_count >= MAX_JOB_ATTEMPTS:
                job.status = AnalysisJobStatus.FAILED.value
                job.completed_at = _now()
                logger.error("job %s failed permanently: %s", job_id, message)
            else:
                job.status = AnalysisJobStatus.PENDING.value
                logger.warning(
                    "job %s failed (attempt %s), will retry: %s",
                    job_id,
                    job.attempt_count,
                    message,
                )
            session.commit()

        self.statistics.failed += 1


# --------------------------------------------------------------------------
# Queue and analysis persistence helpers
# --------------------------------------------------------------------------


def _find_existing_analysis(
    session: Session, job: AnalysisJob, direction: MatchDirection
) -> MatchAnalysis | None:
    """Find the stored analysis for exactly this job's pairing, if any.

    Args:
        session: An open session.
        job: The claimed job, which carries the pairing and the application.
        direction: Which weighting this job asks for.

    Returns:
        The most recently generated matching analysis, or None.
    """
    query = select(MatchAnalysis).where(
        MatchAnalysis.adopter_profile_id == job.adopter_profile_id,
        MatchAnalysis.animal_id == job.animal_id,
        MatchAnalysis.direction == direction.value,
    )
    if job.application_id is None:
        query = query.where(MatchAnalysis.application_id.is_(None))
    else:
        query = query.where(MatchAnalysis.application_id == job.application_id)

    return (
        session.execute(query.order_by(MatchAnalysis.generated_at.desc()).limit(1))
        .scalars()
        .first()
    )


def _is_reusable(session: Session, analysis: MatchAnalysis) -> bool:
    """Whether a stored analysis may be served instead of recomputing it.

    Two conditions, and the second is easy to overlook. The analysis must be
    newer than both records it describes: both must be readable, and a record
    the worker cannot read counts as changed, because inference costs time
    while an explanation of facts that may have moved costs credibility.

    It must also be a real analysis. A row written by the deterministic
    fallback is a *degraded* result - the score with criterion text and no
    model prose - recorded because the model was unavailable at the time.
    Reusing one would make a temporary outage permanent for that pairing, so
    it is recomputed as soon as a model is there to write the explanation.

    Args:
        session: An open session.
        analysis: The stored analysis.

    Returns:
        True when the analysis may be reused unchanged.
    """
    if analysis.model_name == DETERMINISTIC_FALLBACK_MODEL:
        return False

    animal = session.get(Animal, analysis.animal_id)
    adopter = session.get(AdopterProfile, analysis.adopter_profile_id)
    if animal is None or adopter is None:
        return False

    return analysis.generated_at > max(animal.updated_at, adopter.updated_at)


def _analysis_columns(outcome: AnalysisOutcome) -> dict[str, Any]:
    """Render one outcome as the columns of a `match_analyses` row.

    The JSON-shaped columns are NVARCHAR(MAX) because SQL Server 2014 has no
    JSON type, so every list is serialised here rather than in the model.

    Args:
        outcome: What the analysis produced.

    Returns:
        Column names mapped to storable values.
    """
    return {
        "score": outcome.score.score,
        "is_disqualified": outcome.score.is_disqualified,
        "criterion_scores": json.dumps(
            [
                {
                    "criterion": item.criterion.value,
                    "score": item.score,
                    "weight": item.weight,
                    "explanation": item.explanation,
                }
                for item in outcome.score.criterion_scores
            ]
        ),
        "reasons": json.dumps(outcome.reasons),
        "concerns": json.dumps(outcome.concerns),
        "missing_information": json.dumps(outcome.missing_information),
        "evidence_sources": json.dumps(outcome.evidence_sources),
        "reasoning_trace": json.dumps(
            [
                {"step": step.step_number, "action": step.action, "detail": step.detail}
                for step in outcome.reasoning_trace
            ]
        ),
        "used_web_search": outcome.used_web_search,
        "model_name": outcome.model_name,
        "generated_at": _now(),
    }


def _append_analysis_event(
    session: Session,
    job: AnalysisJob,
    outcome: AnalysisOutcome,
    direction: MatchDirection,
    analysis_id: str,
) -> None:
    """Append the `AIAnalysisCompleted` event for a fresh computation.

    Args:
        session: The open transaction the analysis was written in.
        job: The claimed job.
        outcome: What the analysis produced.
        direction: Which weighting was applied.
        analysis_id: The row the event points at.
    """
    EventStore(session).append(
        DomainEventType.AI_ANALYSIS_COMPLETED,
        AggregateType.APPLICATION if job.application_id else AggregateType.ANIMAL,
        job.application_id or job.animal_id or analysis_id,
        payload={
            "match_analysis_id": analysis_id,
            # animal_id lets the dashboard activity feed name the animal.
            # Without it the feed reads "completed a match analysis for"
            # with nothing after it.
            "animal_id": job.animal_id,
            "adopter_profile_id": job.adopter_profile_id,
            "score": outcome.score.score,
            "direction": direction.value,
            "used_web_search": outcome.used_web_search,
            "model_name": outcome.model_name,
        },
        # No actor: the agent is a system component, not a person.
        actor_user_id=None,
    )


def _close_job(session: Session, job_id: str) -> None:
    """Mark one job completed inside the caller's transaction."""
    job = session.get(AnalysisJob, job_id)
    if job is None:
        return
    job.status = AnalysisJobStatus.COMPLETED.value
    job.completed_at = _now()


def _now() -> datetime:
    """Current UTC time, stored naive to match the SQL Server DATETIME columns."""
    return datetime.now(UTC).replace(tzinfo=None)


# --------------------------------------------------------------------------
# Assembly and entry point
# --------------------------------------------------------------------------


def build_language_model(configuration: Configuration) -> LanguageModel:
    """Build the chat model the agent and the intent interpreter share."""
    return OllamaLanguageModel(
        base_url=configuration.agent.ollama_base_url,
        name=configuration.agent.chat_model,
    )


def build_agent(
    configuration: Configuration, language_model: LanguageModel | None = None
) -> MatchAnalysisAgent:
    """Assemble the agent and its tools from configuration.

    Args:
        configuration: Where the model, vector store and search key live.
        language_model: The model to use, built from configuration when not
            supplied. Passed in by the entry point so one model instance
            serves both the agent and intent jobs.

    Returns:
        The assembled agent.
    """
    embedding_client = EmbeddingClient(
        base_url=configuration.agent.ollama_base_url,
        model_name=configuration.agent.embedding_model,
    )
    knowledge_base = KnowledgeBase(
        persist_directory=configuration.chroma_persist_directory,
        collection_name=configuration.rag_collection_name,
        embedding_client=embedding_client,
    )

    return MatchAnalysisAgent(
        language_model=language_model or build_language_model(configuration),
        knowledge_base=knowledge_base,
        mcp_client=McpToolClient(),
        search_provider=build_search_provider(configuration.agent.tavily_api_key),
        max_reasoning_steps=configuration.agent.max_reasoning_steps,
    )


def main() -> int:
    """Start the worker and run until interrupted.

    Returns:
        The process exit code.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
    )

    configuration = load_configuration()
    session_factory = create_session_factory(create_database_engine(configuration))
    language_model = build_language_model(configuration)
    worker = AgentWorker(
        configuration,
        session_factory,
        build_agent(configuration, language_model),
        language_model,
    )

    def handle_signal(_signal_number: int, _frame: FrameType | None) -> None:
        """Stop after the current job rather than mid-write."""
        logger.info("stop requested; finishing current job")
        worker.request_stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    worker.run_forever()
    return 0
