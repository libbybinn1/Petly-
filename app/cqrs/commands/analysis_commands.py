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
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cqrs.base import Command, CommandHandler
from app.cqrs.commands.application_commands import RecordNotFoundError
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
class EnqueueSemanticAdopterSearchCommand(Command):
    """Ask the agent for a vector search of adopter profiles (requirement 6).

    Staff press "Run semantic search" on the discovery screen. Embedding the
    animal and searching the profile index needs an embedding model, which
    the web tier never calls (rule R4), so this queues a
    `SEMANTIC_ADOPTER_SEARCH` job and the page reads the answer back later.

    Attributes:
        animal_id: The animal to find similar adopters for.
    """

    animal_id: str


class EnqueueSemanticAdopterSearchHandler(CommandHandler[str]):
    """Inserts one SEMANTIC_ADOPTER_SEARCH job, or reuses an outstanding one."""

    def handle(self, command: Command, session: Session) -> str:
        """Queue the search, unless one for this animal is already waiting.

        A second press while the first search is still queued returns the
        same job rather than adding another: both would answer the same
        question, and the agent works through the queue one job at a time.

        Returns:
            The identifier of the job that will answer. Not the job row and
            not the matches, which do not exist yet.

        Raises:
            RecordNotFoundError: No such animal.
        """
        assert isinstance(command, EnqueueSemanticAdopterSearchCommand)

        if session.get(Animal, command.animal_id) is None:
            raise RecordNotFoundError(f"animal {command.animal_id} does not exist")

        outstanding_job_id = session.execute(
            select(AnalysisJob.analysis_job_id)
            .where(
                AnalysisJob.animal_id == command.animal_id,
                AnalysisJob.job_type == AnalysisJobType.SEMANTIC_ADOPTER_SEARCH.value,
                AnalysisJob.status.in_(_OUTSTANDING_JOB_STATUSES),
            )
            .limit(1)
        ).scalar_one_or_none()
        if outstanding_job_id is not None:
            return outstanding_job_id

        job_id = new_identifier()
        session.add(
            AnalysisJob(
                analysis_job_id=job_id,
                job_type=AnalysisJobType.SEMANTIC_ADOPTER_SEARCH.value,
                status=AnalysisJobStatus.PENDING.value,
                animal_id=command.animal_id,
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

        # dict.fromkeys de-duplicates while keeping the ranking order, so the
        # jobs are inserted best-match first and the agent explains the
        # animals the adopter is most likely to look at soonest.
        animal_ids = tuple(dict.fromkeys(command.animal_ids))
        if not animal_ids:
            return 0

        needed = _animal_ids_needing_explanation(
            session, command.adopter_profile_id, animal_ids
        )
        for animal_id in needed:
            _queue_find_my_pet_job(session, command.adopter_profile_id, animal_id)
        return len(needed)


def _animal_ids_needing_explanation(
    session: Session, adopter_profile_id: str, animal_ids: tuple[str, ...]
) -> list[str]:
    """Decide which pairings still need a job, for the whole ranking at once.

    Asked one animal at a time this cost four round trips per animal, and the
    database is remote: ranking forty animals meant well over a hundred of
    them in a single page load (docs/ARCHITECTURE.md §10). The answer is the
    same; it is now reached in three queries regardless of how many animals
    were ranked.

    Args:
        session: The session to read through.
        adopter_profile_id: The adopter the ranking belongs to.
        animal_ids: Ranked animals, already de-duplicated, best first.

    Returns:
        The animal ids still needing a queued explanation, in ranking order.
    """
    outstanding = _animal_ids_with_outstanding_job(session, adopter_profile_id, animal_ids)
    candidates = tuple(
        animal_id for animal_id in animal_ids if animal_id not in outstanding
    )
    if not candidates:
        return []

    reusable = _animal_ids_with_reusable_analysis(session, adopter_profile_id, candidates)
    return [animal_id for animal_id in candidates if animal_id not in reusable]


def _animal_ids_with_outstanding_job(
    session: Session, adopter_profile_id: str, animal_ids: tuple[str, ...]
) -> set[str]:
    """Which of these animals the agent has already been asked about.

    Args:
        session: The session to read through.
        adopter_profile_id: The adopter the jobs belong to.
        animal_ids: The animals to check.

    Returns:
        The subset with a job that has not finished.
    """
    rows = session.execute(
        select(AnalysisJob.animal_id).where(
            AnalysisJob.adopter_profile_id == adopter_profile_id,
            AnalysisJob.animal_id.in_(animal_ids),
            AnalysisJob.job_type == AnalysisJobType.FIND_MY_PET.value,
            AnalysisJob.status.in_(_OUTSTANDING_JOB_STATUSES),
        )
    ).scalars()
    # A job row may carry a null animal_id for the free-text search type,
    # which this filter never selects; the guard keeps the set typed.
    return {animal_id for animal_id in rows if animal_id is not None}


def _animal_ids_with_reusable_analysis(
    session: Session, adopter_profile_id: str, animal_ids: tuple[str, ...]
) -> set[str]:
    """Which of these animals already have an explanation worth keeping.

    Mirrors the worker's reuse rule unchanged: the row must be model prose,
    and it must be newer than both records it describes. A deterministic
    fallback is never reusable - it is recomputed once a model can write the
    explanation.

    Args:
        session: The session to read through.
        adopter_profile_id: The adopter the analyses belong to.
        animal_ids: The animals to check.

    Returns:
        The subset whose stored analysis may stand in for a new job.
    """
    adopter = session.get(AdopterProfile, adopter_profile_id)
    if adopter is None:
        return set()

    newest_analyses = _newest_analysis_per_animal(session, adopter_profile_id, animal_ids)
    animal_updated_at: dict[str, datetime] = dict(
        session.execute(
            select(Animal.animal_id, Animal.updated_at).where(
                Animal.animal_id.in_(animal_ids)
            )
        )
        .tuples()
        .all()
    )

    return {
        animal_id
        for animal_id, analysis in newest_analyses.items()
        if analysis.model_name != DETERMINISTIC_FALLBACK_MODEL
        and animal_id in animal_updated_at
        and analysis.generated_at > max(animal_updated_at[animal_id], adopter.updated_at)
    }


def _newest_analysis_per_animal(
    session: Session, adopter_profile_id: str, animal_ids: tuple[str, ...]
) -> dict[str, MatchAnalysis]:
    """Read the most recent adopter-side analysis for each animal.

    Ordered newest first so the first row seen for an animal is the one that
    counts; `setdefault` then keeps it and ignores the older rows. SQL Server
    2014 has no `DISTINCT ON`, and a window function here would be harder to
    read for no measurable gain at this row count.

    Args:
        session: The session to read through.
        adopter_profile_id: The adopter the analyses belong to.
        animal_ids: The animals to read analyses for.

    Returns:
        The newest analysis per animal, for animals that have one.
    """
    analyses = session.execute(
        select(MatchAnalysis)
        .where(
            MatchAnalysis.adopter_profile_id == adopter_profile_id,
            MatchAnalysis.animal_id.in_(animal_ids),
            MatchAnalysis.direction == MatchDirection.ADOPTER_TO_ANIMAL.value,
        )
        .order_by(MatchAnalysis.generated_at.desc())
    ).scalars()

    newest: dict[str, MatchAnalysis] = {}
    for analysis in analyses:
        newest.setdefault(analysis.animal_id, analysis)
    return newest


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


@dataclass(frozen=True)
class EnqueueFindMoreAdoptersExplanationsCommand(Command):
    """Ask the agent to explain the adopters just ranked for one animal in discovery.

    Find More Adopters scores deterministically. The prose under "Why this could work"
    is written by the worker as an `ANIMAL_TO_ADOPTER` analysis under `FIND_MORE_ADOPTERS`.

    Attributes:
        animal_id: The animal whose discovery screen was opened.
        adopter_profile_ids: The adopters on the page, in rank order. Pairs that
            already have outstanding work, or a reusable analysis, are skipped.
    """

    animal_id: str
    adopter_profile_ids: tuple[str, ...]


class EnqueueFindMoreAdoptersExplanationsHandler(CommandHandler[int]):
    """Inserts one FIND_MORE_ADOPTERS job per adopter that still needs an explanation."""

    def handle(self, command: Command, session: Session) -> int:
        """Queue the missing explanations and return how many were inserted."""
        assert isinstance(command, EnqueueFindMoreAdoptersExplanationsCommand)

        adopter_profile_ids = tuple(dict.fromkeys(command.adopter_profile_ids))
        if not adopter_profile_ids:
            return 0

        needed = _adopter_ids_needing_explanation(
            session, command.animal_id, adopter_profile_ids
        )
        for adopter_id in needed:
            _queue_find_more_adopters_job(session, adopter_id, command.animal_id)
        return len(needed)


def _adopter_ids_needing_explanation(
    session: Session, animal_id: str, adopter_profile_ids: tuple[str, ...]
) -> list[str]:
    """Decide which candidate adopters still need an explanation job."""
    outstanding = _adopter_ids_with_outstanding_job(session, animal_id, adopter_profile_ids)
    candidates = tuple(
        adopter_id for adopter_id in adopter_profile_ids if adopter_id not in outstanding
    )
    if not candidates:
        return []

    reusable = _adopter_ids_with_reusable_analysis(session, animal_id, candidates)
    return [adopter_id for adopter_id in candidates if adopter_id not in reusable]


def _adopter_ids_with_outstanding_job(
    session: Session, animal_id: str, adopter_profile_ids: tuple[str, ...]
) -> set[str]:
    """Which of these adopters the agent has already been asked about."""
    rows = session.execute(
        select(AnalysisJob.adopter_profile_id).where(
            AnalysisJob.animal_id == animal_id,
            AnalysisJob.adopter_profile_id.in_(adopter_profile_ids),
            AnalysisJob.job_type.in_((
                AnalysisJobType.FIND_MORE_ADOPTERS.value,
                AnalysisJobType.RANK_APPLICANT.value,
            )),
            AnalysisJob.status.in_(_OUTSTANDING_JOB_STATUSES),
        )
    ).scalars()
    return {adopter_id for adopter_id in rows if adopter_id is not None}


def _adopter_ids_with_reusable_analysis(
    session: Session, animal_id: str, adopter_profile_ids: tuple[str, ...]
) -> set[str]:
    """Which of these adopters already have an explanation worth keeping."""
    animal = session.get(Animal, animal_id)
    if animal is None:
        return set()

    newest_analyses = _newest_analysis_per_adopter(session, animal_id, adopter_profile_ids)
    adopter_updated_at: dict[str, datetime] = dict(
        session.execute(
            select(AdopterProfile.adopter_profile_id, AdopterProfile.updated_at).where(
                AdopterProfile.adopter_profile_id.in_(adopter_profile_ids)
            )
        )
        .tuples()
        .all()
    )

    return {
        adopter_id
        for adopter_id, analysis in newest_analyses.items()
        if analysis.model_name != DETERMINISTIC_FALLBACK_MODEL
        and adopter_id in adopter_updated_at
        and analysis.generated_at > max(adopter_updated_at[adopter_id], animal.updated_at)
    }


def _newest_analysis_per_adopter(
    session: Session, animal_id: str, adopter_profile_ids: tuple[str, ...]
) -> dict[str, MatchAnalysis]:
    """Read the most recent staff-side analysis for each adopter."""
    analyses = session.execute(
        select(MatchAnalysis)
        .where(
            MatchAnalysis.animal_id == animal_id,
            MatchAnalysis.adopter_profile_id.in_(adopter_profile_ids),
            MatchAnalysis.direction == MatchDirection.ANIMAL_TO_ADOPTER.value,
        )
        .order_by(MatchAnalysis.generated_at.desc())
    ).scalars()

    newest: dict[str, MatchAnalysis] = {}
    for analysis in analyses:
        newest.setdefault(analysis.adopter_profile_id, analysis)
    return newest


def _queue_find_more_adopters_job(
    session: Session, adopter_profile_id: str, animal_id: str
) -> None:
    """Insert one pending Find More Adopters job for a single pairing."""
    session.add(
        AnalysisJob(
            analysis_job_id=new_identifier(),
            job_type=AnalysisJobType.FIND_MORE_ADOPTERS.value,
            status=AnalysisJobStatus.PENDING.value,
            adopter_profile_id=adopter_profile_id,
            animal_id=animal_id,
            attempt_count=0,
            created_at=utc_now(),
        )
    )

