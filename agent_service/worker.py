"""The independent agent process: claims jobs and records analyses.

This is the process boundary that makes the agent genuinely separate
(blueprint requirement 4.5). It polls the `analysis_jobs` table, runs the
agent loop, writes a `MatchAnalysis` and appends `AIAnalysisCompleted`. It
never imports the Flask application.

Run it with:

    .venv/Scripts/python.exe -m agent_service
"""

from __future__ import annotations

import json
import logging
import signal
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from types import FrameType

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
from app.infrastructure.models import AnalysisJob, MatchAnalysis, new_identifier
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from agent_service.llm_client import LanguageModel, OllamaLanguageModel
from agent_service.loop import AnalysisOutcome, MatchAnalysisAgent, MissingRecordError
from agent_service.rag.knowledge_base import EmbeddingClient, KnowledgeBase
from agent_service.tools.mcp_tools import McpToolClient
from agent_service.tools.web_search import build_search_provider

logger = logging.getLogger("petmatch.agent")

# A job that keeps failing is parked rather than retried forever, so one bad
# record cannot monopolise the worker.
MAX_JOB_ATTEMPTS = 3

DIRECTION_BY_JOB_TYPE = {
    AnalysisJobType.RANK_APPLICANT: MatchDirection.ANIMAL_TO_ADOPTER,
    AnalysisJobType.FIND_MORE_ADOPTERS: MatchDirection.ANIMAL_TO_ADOPTER,
    AnalysisJobType.FIND_MY_PET: MatchDirection.ADOPTER_TO_ANIMAL,
}


@dataclass
class WorkerStatistics:
    """Counters reported when the worker stops."""

    completed: int = 0
    failed: int = 0
    skipped: int = 0


class AgentWorker:
    """Polls the job queue and runs analyses until asked to stop."""

    def __init__(
        self,
        configuration: Configuration,
        session_factory: sessionmaker[Session],
        agent: MatchAnalysisAgent,
    ) -> None:
        """Wire the worker to its queue and agent."""
        self._configuration = configuration
        self._session_factory = session_factory
        self._agent = agent
        self._should_stop = False
        self.statistics = WorkerStatistics()

    def request_stop(self) -> None:
        """Ask the loop to finish the current job and exit."""
        self._should_stop = True

    def run_forever(self) -> WorkerStatistics:
        """Poll until asked to stop."""
        interval = self._configuration.agent.poll_interval_seconds
        logger.info("agent worker started, polling every %ss", interval)

        while not self._should_stop:
            if not self.run_once():
                time.sleep(interval)

        logger.info(
            "agent worker stopped: %s completed, %s failed, %s skipped",
            self.statistics.completed,
            self.statistics.failed,
            self.statistics.skipped,
        )
        return self.statistics

    def run_once(self) -> bool:
        """Claim and process at most one job.

        Returns:
            True when a job was processed, False when the queue was empty.
        """
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

    def _claim_next_job(self) -> AnalysisJob | None:
        """Take the oldest pending job and mark it in progress.

        Claiming is a committed write before any work begins, so a second
        worker cannot pick up the same job.
        """
        with self._session_factory() as session:
            job = session.execute(
                select(AnalysisJob)
                .where(AnalysisJob.status == AnalysisJobStatus.PENDING.value)
                .order_by(AnalysisJob.created_at)
                .limit(1)
            ).scalar_one_or_none()

            if job is None:
                return None

            job.status = AnalysisJobStatus.IN_PROGRESS.value
            job.started_at = _now()
            job.attempt_count += 1
            session.commit()
            session.refresh(job)
            session.expunge(job)
            return job

    def _process(self, job: AnalysisJob) -> None:
        """Run the analysis for one claimed job."""
        job_type = AnalysisJobType(job.job_type)
        direction = DIRECTION_BY_JOB_TYPE.get(job_type)

        if direction is None or not job.adopter_profile_id or not job.animal_id:
            self._finish_failed(
                job.analysis_job_id,
                f"job type {job.job_type} needs both an adopter and an animal",
            )
            self.statistics.skipped += 1
            return

        outcome = self._agent.analyse(job.adopter_profile_id, job.animal_id, direction)
        self._persist(job, outcome, direction)
        self.statistics.completed += 1
        logger.info(
            "job %s complete: score=%s model=%s",
            job.analysis_job_id,
            outcome.score.score,
            outcome.model_name,
        )

    def _persist(
        self, job: AnalysisJob, outcome: AnalysisOutcome, direction: MatchDirection
    ) -> None:
        """Write the analysis, append its event and close the job.

        All three happen in one transaction: an analysis without its event, or
        a job marked complete with nothing stored, would both be worse than a
        clean retry.
        """
        with self._session_factory() as session:
            analysis_id = new_identifier()

            session.add(
                MatchAnalysis(
                    match_analysis_id=analysis_id,
                    direction=direction.value,
                    adopter_profile_id=job.adopter_profile_id,
                    animal_id=job.animal_id,
                    application_id=job.application_id,
                    score=outcome.score.score,
                    is_disqualified=outcome.score.is_disqualified,
                    # NVARCHAR(MAX) columns: SQL Server 2014 has no JSON type.
                    criterion_scores=json.dumps(
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
                    reasons=json.dumps(outcome.reasons),
                    concerns=json.dumps(outcome.concerns),
                    missing_information=json.dumps(outcome.missing_information),
                    evidence_sources=json.dumps(outcome.evidence_sources),
                    used_web_search=outcome.used_web_search,
                    model_name=outcome.model_name,
                    generated_at=_now(),
                )
            )

            EventStore(session).append(
                DomainEventType.AI_ANALYSIS_COMPLETED,
                AggregateType.APPLICATION if job.application_id else AggregateType.ANIMAL,
                job.application_id or job.animal_id or analysis_id,
                payload={
                    "match_analysis_id": analysis_id,
                    "score": outcome.score.score,
                    "direction": direction.value,
                    "used_web_search": outcome.used_web_search,
                    "model_name": outcome.model_name,
                },
                # No actor: the agent is a system component, not a person.
                actor_user_id=None,
            )

            queued = session.get(AnalysisJob, job.analysis_job_id)
            if queued is not None:
                queued.status = AnalysisJobStatus.COMPLETED.value
                queued.completed_at = _now()

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


def _now() -> datetime:
    """Current UTC time, stored naive to match the SQL Server DATETIME columns."""
    return datetime.now(UTC).replace(tzinfo=None)


def build_agent(configuration: Configuration) -> MatchAnalysisAgent:
    """Assemble the agent and its tools from configuration."""
    embedding_client = EmbeddingClient(
        base_url=configuration.agent.ollama_base_url,
        model_name=configuration.agent.embedding_model,
    )
    knowledge_base = KnowledgeBase(
        persist_directory=configuration.chroma_persist_directory,
        collection_name=configuration.rag_collection_name,
        embedding_client=embedding_client,
    )
    language_model: LanguageModel = OllamaLanguageModel(
        base_url=configuration.agent.ollama_base_url,
        name=configuration.agent.chat_model,
    )

    return MatchAnalysisAgent(
        language_model=language_model,
        knowledge_base=knowledge_base,
        mcp_client=McpToolClient(),
        search_provider=build_search_provider(configuration.agent.tavily_api_key),
        max_reasoning_steps=configuration.agent.max_reasoning_steps,
    )


def main() -> int:
    """Start the worker and run until interrupted."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
    )

    configuration = load_configuration()
    session_factory = create_session_factory(create_database_engine(configuration))
    worker = AgentWorker(configuration, session_factory, build_agent(configuration))

    def handle_signal(_signal_number: int, _frame: FrameType | None) -> None:
        """Stop after the current job rather than mid-write."""
        logger.info("stop requested; finishing current job")
        worker.request_stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    worker.run_forever()
    return 0
