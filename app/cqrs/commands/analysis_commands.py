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

Per the CQRS contract every handler here returns an identifier - never the
job it created, and never the answer, which does not exist yet
(blueprint section 9.2).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.cqrs.base import Command, CommandHandler
from app.domain.enums import AnalysisJobStatus, AnalysisJobType
from app.infrastructure.clock import utc_now
from app.infrastructure.models import AnalysisJob, new_identifier

# Matches the declared width of `analysis_jobs.natural_language_query` with
# room to spare, and is long enough for any real description. The cap is the
# point: this route is open to signed-out visitors, and the text becomes a
# model prompt in another process, where its length is directly a cost.
MAXIMUM_DESCRIPTION_LENGTH = 500



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
