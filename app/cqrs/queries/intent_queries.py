"""Read side of the asynchronous natural-language search (spec section 6.3).

The web tier asks the agent a question by inserting an `INTERPRET_INTENT` job
and redirecting; this module is how it reads the answer back. The contract on
the wire is the JSON in `analysis_jobs.result_payload`, documented in
docs/AGENT.md section 11 and owned by `app.domain.search_intent`, the shared
vocabulary both processes import. The payload is decoded through
`payload_to_intent` rather than by picking keys apart here - the web tier
validates what it did not write, and nothing under `app/` imports any agent
module (rule R2).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.cqrs.base import Query, QueryHandler
from app.cqrs.queries.animal_queries import AnimalSearchFilters
from app.cqrs.queries.formatting import DisplayDate, to_display_moment
from app.domain.enums import AnalysisJobStatus, AnalysisJobType
from app.domain.search_intent import SearchIntent, payload_to_intent
from app.infrastructure.models import AnalysisJob


class IntentJobNotYoursError(PermissionError):
    """Raised when a visitor asks for an interpretation belonging to somebody else."""


@dataclass(frozen=True)
class InterpretedIntentView:
    """One natural-language search, as the describe screen sees it.

    Attributes:
        analysis_job_id: The job, which is also this page's address.
        status: Where the agent has got to.
        described: What the visitor originally typed, so the form can be
            re-rendered with it and the page can quote it back.
        adopter_profile_id: The profile to fuse the intent with (spec section
            6.4), or None for an intent-only search.
        created_at: When the job was queued.
        intent: The parsed criteria, present only once the job COMPLETED.
    """

    analysis_job_id: str
    status: AnalysisJobStatus
    described: str
    adopter_profile_id: str | None
    created_at: datetime
    intent: SearchIntent | None = None

    @property
    def is_waiting(self) -> bool:
        """Whether the agent has not finished with this yet.

        IN_PROGRESS counts as waiting because the page is waiting on it just
        the same, and a job the worker returns to the queue after a failed
        attempt goes back to PENDING - so a retry keeps the page waiting
        rather than showing a failure that is still being worked on.
        """
        return self.status in (AnalysisJobStatus.PENDING, AnalysisJobStatus.IN_PROGRESS)

    @property
    def has_failed(self) -> bool:
        """Whether the agent gave up on this request."""
        return self.status is AnalysisJobStatus.FAILED

    @property
    def is_ready(self) -> bool:
        """Whether there is an interpretation to search with."""
        return self.status is AnalysisJobStatus.COMPLETED and self.intent is not None

    @property
    def uses_profile(self) -> bool:
        """Whether results will be ranked against a stored profile as well."""
        return self.adopter_profile_id is not None

    @property
    def queued_at(self) -> DisplayDate:
        """When this was queued, in the three forms a screen needs."""
        return to_display_moment(self.created_at)


@dataclass(frozen=True)
class GetIntentJobQuery(Query):
    """Fetch one natural-language search by its job identifier.

    Carries the viewer rather than only the record, because the answer
    depends on who is asking: an interpretation bound to a profile is
    personal data (FR-2.4). The handler decides; the controller only maps
    the outcome onto a status code.

    Attributes:
        analysis_job_id: The job to read.
        viewer_adopter_profile_id: The signed-in adopter's profile, or None
            for a signed-out visitor or a staff member.
        viewer_is_staff: Whether the viewer may see anybody's search.
    """

    analysis_job_id: str
    viewer_adopter_profile_id: str | None = None
    viewer_is_staff: bool = False


class GetIntentJobHandler(QueryHandler[InterpretedIntentView | None]):
    """Answers GetIntentJobQuery."""

    def handle(self, query: Query, session: Session) -> InterpretedIntentView | None:
        """Return one interpretation job, if the viewer may see it.

        Ownership lives here rather than in the controller so the rule
        travels with the data (rule R2, and the same reasoning as
        `app.cqrs.queries.personal_queries`). A job carrying no profile is
        an anonymous search, and holding its identifier is the whole of the
        claim to it - there is nothing personal in it to protect.

        Returns:
            The view model, or None when no such interpretation job exists.

        Raises:
            IntentJobNotYoursError: The job is bound to a different adopter's
                profile and the viewer is not staff.
        """
        assert isinstance(query, GetIntentJobQuery)

        job = session.get(AnalysisJob, query.analysis_job_id)
        if job is None or job.job_type != AnalysisJobType.INTERPRET_INTENT.value:
            # A job of another type is reported as absent rather than as
            # forbidden: this route describes searches, and an analysis job
            # identifier is simply not one of them.
            return None

        self._ensure_visible(job, query)

        status = AnalysisJobStatus(job.status)
        return InterpretedIntentView(
            analysis_job_id=job.analysis_job_id,
            status=status,
            described=job.natural_language_query or "",
            adopter_profile_id=job.adopter_profile_id,
            created_at=job.created_at,
            intent=(
                _decode_intent(job.result_payload)
                if status is AnalysisJobStatus.COMPLETED
                else None
            ),
        )

    @staticmethod
    def _ensure_visible(job: AnalysisJob, query: GetIntentJobQuery) -> None:
        """Raise unless this viewer may read this job.

        Raises:
            IntentJobNotYoursError: The job belongs to another adopter.
        """
        if job.adopter_profile_id is None or query.viewer_is_staff:
            return
        if job.adopter_profile_id == query.viewer_adopter_profile_id:
            return
        raise IntentJobNotYoursError("This search belongs to another adopter.")


def filters_from_intent(intent: SearchIntent) -> AnimalSearchFilters:
    """Turn interpreted criteria into the deterministic search's filters.

    The single point where model output becomes a database filter, so the
    intent-only search and the profile-fused one cannot narrow differently
    (spec section 6.4). Nothing the model produced selects or scores an
    animal: it only chooses which rows the ordinary search considers.

    A criterion the visitor did not express stays absent, meaning "no
    constraint" - guessing would quietly rule animals out with nothing on
    screen to explain why (spec section 6.3).

    Args:
        intent: Criteria already validated against their enums by
            `app.domain.search_intent`.

    Returns:
        Filters for `SearchAnimalsQuery` or `FindMyPetWithIntentQuery`.
    """
    return AnimalSearchFilters(
        # One species is a filter; several are a disjunction the structured
        # search has no column for, so the broader answer is the honest one.
        species=intent.species[0].value if len(intent.species) == 1 else None,
        size=intent.size.value if intent.size is not None else None,
        activity_level=intent.activity_level.value if intent.activity_level is not None else None,
        temperament=intent.temperament.value if intent.temperament is not None else None,
        good_with_children=bool(intent.good_with_children),
        good_with_other_animals=bool(intent.good_with_other_animals),
        available_only=True,
    )


def _decode_intent(raw_payload: str | None) -> SearchIntent | None:
    """Read the stored interpretation, tolerating a payload that makes no sense.

    The column is NVARCHAR(MAX) because SQL Server 2014 has no JSON type, so
    what comes back is a string that has been through another process. A
    completed job with unreadable content is treated as not understood, which
    the screen already knows how to explain, rather than as a 500.

    Args:
        raw_payload: The job's `result_payload`.

    Returns:
        The parsed intent, or None when there was nothing to parse.
    """
    if not raw_payload:
        return None
    try:
        decoded = json.loads(raw_payload)
    except json.JSONDecodeError:
        return SearchIntent.not_understood("That interpretation could not be read back.")
    if not isinstance(decoded, dict):
        return SearchIntent.not_understood("That interpretation could not be read back.")
    return payload_to_intent(decoded)
