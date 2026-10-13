"""Commands that queue work for the independent agent process.

The `analysis_jobs` table is the process boundary (rule R2): the web tier
inserts a row and returns, and the agent - a separate OS process - claims it,
runs the model and writes the answer back. Nothing here imports the agent.

This module exists because of one measured fact. CPU-only inference on this
machine takes 11 to 16 seconds a call (CLAUDE.md R4), so an interpretation
performed inside a request would hold a worker thread for that long. Spec
section 6.3 wants free-text search anyway, and NFR-3.1 forbids the obvious
implementation, which leaves exactly one design: enqueue, redirect, and let
the page wait where waiting is cheap.

Per the CQRS contract a handler returns an identifier or a count of the rows
it inserted - never the job it created, and never the answer, which does not
exist yet (blueprint section 9.2).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cqrs.base import Command, CommandHandler
from app.domain.enums import AnalysisJobStatus, AnalysisJobType, MatchDirection
from app.infrastructure.clock import utc_now
from app.infrastructure.models import (
    AdopterProfile,
    AnalysisJob,
    Animal,
    MatchAnalysis,
    new_identifier,
)

# Matches the declared width of `analysis_jobs.natural_language_query` with
# room to spare, and is long enough for any real description. The cap is the
# point: this route is open to signed-out visitors, and the text becomes a
# model prompt in another process, where its length is directly a cost.
MAXIMUM_DESCRIPTION_LENGTH = 500

# The same label `agent_service.explanation` records when the scorer's own
# sentences stand in for model prose. Duplicated here so this command does
# not import the agent (rule R2). A fallback row is not reusable: leaving it
# in place would make a temporary model outage permanent for that pairing.
DETERMINISTIC_FALLBACK_MODEL = "deterministic-fallback"

_OUTSTANDING_JOB_STATUSES = (
    AnalysisJobStatus.PENDING.value,
    AnalysisJobStatus.IN_PROGRESS.value,
)



@dataclass(frozen=True)
class EnqueueIntentInterpretationCommand(Command):
    """Ask the agent to interpret one adopter's own words (spec section 6.3).

    Attributes:
        natural_language_query: What the visitor typed, already trimmed and
            length-checked by the controller.
        adopter_profile_id: Whose stable profile to fuse with the resulting
            intent (spec section 6.4), or None for an intent-only search. A
            signed-out visitor always has None.
    """

    natural_language_query: str
    adopter_profile_id: str | None = None


class EnqueueIntentInterpretationHandler(CommandHandler[str]):
    """Inserts one INTERPRET_INTENT job for the agent to claim."""

    def handle(self, command: Command, session: Session) -> str:
        """Queue the interpretation.

        Deliberately does no validation of the text beyond storing it. What
        makes a description acceptable is an HTTP concern - a blank textarea
        and an oversized payload are both answered with 400 by the
        controller - and what makes it *interpretable* is the model's
        problem, answered by the job failing with a reason.

        Returns:
            The new job's identifier, which is also the URL the caller
            redirects to. Not the job row: a command returns no read data.
        """
        assert isinstance(command, EnqueueIntentInterpretationCommand)

        job_id = new_identifier()
        session.add(
            AnalysisJob(
                analysis_job_id=job_id,
                job_type=AnalysisJobType.INTERPRET_INTENT.value,
                status=AnalysisJobStatus.PENDING.value,
                adopter_profile_id=command.adopter_profile_id,
                natural_language_query=command.natural_language_query,
                attempt_count=0,
                created_at=utc_now(),
            )
        )
        return job_id


@dataclass(frozen=True)
class EnqueueFindMyPetExplanationsCommand(Command):
    """Ask the agent to explain the animals just ranked for one adopter.

    Find My Pet scores synchronously (spec section 6.1, NFR-3.2). The prose
    under "Why this animal could suit you" is a `FIND_MY_PET` job, which the
    worker writes as an `ADOPTER_TO_ANIMAL` analysis. A `RANK_APPLICANT` job
    explains the same pair to staff and never fills this screen.

    Attributes:
        adopter_profile_id: Whose matches were just ranked.
        animal_ids: The animals on the page, in rank order. Pairs that
            already have outstanding work, or a reusable analysis, are
            skipped.
    """

    adopter_profile_id: str
    animal_ids: tuple[str, ...]


class EnqueueFindMyPetExplanationsHandler(CommandHandler[int]):
    """Inserts one FIND_MY_PET job per animal that still needs an explanation."""

    def handle(self, command: Command, session: Session) -> int:
        """Queue the missing explanations and return how many were inserted.

        Returns:
            The number of new jobs. Zero when every pair is already queued
            or already explained. Not the jobs themselves.
        """
        assert isinstance(command, EnqueueFindMyPetExplanationsCommand)

        queued_count = 0
        seen_animal_ids: set[str] = set()
        for animal_id in command.animal_ids:
            if animal_id in seen_animal_ids:
                continue
            seen_animal_ids.add(animal_id)
            if not _explanation_is_needed(session, command.adopter_profile_id, animal_id):
                continue
            _queue_find_my_pet_job(session, command.adopter_profile_id, animal_id)
            queued_count += 1
        return queued_count


def _explanation_is_needed(session: Session, adopter_profile_id: str, animal_id: str) -> bool:
    """Whether this pairing still needs a Find My Pet explanation queued."""
    if _find_my_pet_job_is_outstanding(session, adopter_profile_id, animal_id):
        return False
    return not _analysis_is_reusable(session, adopter_profile_id, animal_id)


def _find_my_pet_job_is_outstanding(
    session: Session, adopter_profile_id: str, animal_id: str
) -> bool:
    """Whether the agent has already been asked and has not finished."""
    existing_job_id = session.execute(
        select(AnalysisJob.analysis_job_id)
        .where(
            AnalysisJob.adopter_profile_id == adopter_profile_id,
            AnalysisJob.animal_id == animal_id,
            AnalysisJob.job_type == AnalysisJobType.FIND_MY_PET.value,
            AnalysisJob.status.in_(_OUTSTANDING_JOB_STATUSES),
        )
        .limit(1)
    ).scalar_one_or_none()
    return existing_job_id is not None


def _analysis_is_reusable(session: Session, adopter_profile_id: str, animal_id: str) -> bool:
    """Whether a stored adopter-side analysis may stand in for a new job.

    Mirrors the worker's reuse rule: the row must be model prose, and it
    must be newer than both records it describes. A deterministic fallback
    is recomputed once a model can write the explanation.
    """
    analysis = session.execute(
        select(MatchAnalysis)
        .where(
            MatchAnalysis.adopter_profile_id == adopter_profile_id,
            MatchAnalysis.animal_id == animal_id,
            MatchAnalysis.direction == MatchDirection.ADOPTER_TO_ANIMAL.value,
        )
        .order_by(MatchAnalysis.generated_at.desc())
        .limit(1)
    ).scalars().first()
    if analysis is None or analysis.model_name == DETERMINISTIC_FALLBACK_MODEL:
        return False

    animal = session.get(Animal, animal_id)
    adopter = session.get(AdopterProfile, adopter_profile_id)
    if animal is None or adopter is None:
        return False
    return analysis.generated_at > max(animal.updated_at, adopter.updated_at)


def _queue_find_my_pet_job(session: Session, adopter_profile_id: str, animal_id: str) -> None:
    """Insert one pending Find My Pet job for a single pairing."""
    session.add(
        AnalysisJob(
            analysis_job_id=new_identifier(),
            job_type=AnalysisJobType.FIND_MY_PET.value,
            status=AnalysisJobStatus.PENDING.value,
            adopter_profile_id=adopter_profile_id,
            animal_id=animal_id,
            attempt_count=0,
            created_at=utc_now(),
        )
    )
