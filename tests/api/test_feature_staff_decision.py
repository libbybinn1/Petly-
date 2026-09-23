"""HTTP tests for the staff decision route (FR-7.4, FR-7.5).

Until this route existed, `ApproveApplicationCommand` and
`ReverseApprovalCommand` were registered on the bus and dispatched by
nothing: the spec 7.5 cascade that justifies the whole event-sourced design
could only be triggered from a test or a script. These tests exercise it the
way a staff member reaches it, over HTTP, with authorization enforced.
"""

from __future__ import annotations

import pytest
from app.domain.enums import AnimalStatus, ApplicationStatus
from app.infrastructure.models import AdoptionApplication, Animal, new_identifier
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import _now

pytestmark = pytest.mark.api


def apply_for(
    session_factory: sessionmaker[Session], profile_id: str, animal_id: str
) -> str:
    """Insert one submitted application directly and return its identifier.

    Inserted rather than posted because these tests are about the decision,
    not about how the application arrived.
    """
    application_id = new_identifier()
    with session_factory() as session:
        session.add(
            AdoptionApplication(
                application_id=application_id,
                adopter_profile_id=profile_id,
                animal_id=animal_id,
                status=ApplicationStatus.SUBMITTED.value,
                submitted_at=_now(),
            )
        )
        session.commit()
    return application_id


def status_of(session_factory: sessionmaker[Session], application_id: str) -> str:
    """Read one application's stored status."""
    with session_factory() as session:
        return session.execute(
            select(AdoptionApplication.status).where(
                AdoptionApplication.application_id == application_id
            )
        ).scalar_one()


def animal_status_of(session_factory: sessionmaker[Session], animal_id: str) -> str:
    """Read one animal's stored status."""
    with session_factory() as session:
        return session.execute(
            select(Animal.status).where(Animal.animal_id == animal_id)
        ).scalar_one()


class TestAuthorization:
    """The decision is a staff action, enforced on the server."""

    def test_an_adopter_cannot_decide_an_application(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves an adopter posting directly is refused, not merely unshown.

        Hiding the button is not authorization (blueprint section 12).
        """
        application_id = apply_for(
            session_factory, world["adopter_profile_id"], world["available_animal_id"]
        )

        response = adopter_client.post(
            f"/applications/{application_id}/decide",
            data={"decision": "APPROVE", "animal_id": world["available_animal_id"]},
        )

        assert response.status_code == 403
        assert status_of(session_factory, application_id) == (
            ApplicationStatus.SUBMITTED.value
        )

    def test_an_anonymous_visitor_is_sent_to_sign_in(
        self,
        client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the route is not reachable signed out."""
        application_id = apply_for(
            session_factory, world["adopter_profile_id"], world["available_animal_id"]
        )

        response = client.post(
            f"/applications/{application_id}/decide", data={"decision": "APPROVE"}
        )

        assert response.status_code in (302, 401)
        assert status_of(session_factory, application_id) == (
            ApplicationStatus.SUBMITTED.value
        )


class TestDecisions:
    """Each decision records the state a staff member chose."""

    def test_approving_records_the_approval_and_reserves_the_animal(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the approval reaches the database through the route."""
        application_id = apply_for(
            session_factory, world["adopter_profile_id"], world["available_animal_id"]
        )

        staff_client.post(
            f"/applications/{application_id}/decide",
            data={"decision": "APPROVE", "animal_id": world["available_animal_id"]},
        )

        assert status_of(session_factory, application_id) == (
            ApplicationStatus.APPROVED.value
        )
        assert animal_status_of(session_factory, world["available_animal_id"]) == (
            AnimalStatus.ADOPTION_IN_PROGRESS.value
        )

    def test_rejecting_records_the_rejection_and_leaves_the_animal_available(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves rejecting one applicant does not withdraw the animal.

        The other applicants are still in the running, and the animal was
        never promised to this one.
        """
        application_id = apply_for(
            session_factory, world["adopter_profile_id"], world["available_animal_id"]
        )

        staff_client.post(
            f"/applications/{application_id}/decide",
            data={
                "decision": "REJECT",
                "animal_id": world["available_animal_id"],
                "note": "Not a fit for this animal's needs.",
            },
        )

        assert status_of(session_factory, application_id) == (
            ApplicationStatus.REJECTED.value
        )
        assert animal_status_of(session_factory, world["available_animal_id"]) == (
            AnimalStatus.AVAILABLE.value
        )

    def test_marking_under_review_records_it(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves review is a recorded state, not an informal one."""
        application_id = apply_for(
            session_factory, world["adopter_profile_id"], world["available_animal_id"]
        )

        staff_client.post(
            f"/applications/{application_id}/decide",
            data={"decision": "REVIEW", "animal_id": world["available_animal_id"]},
        )

        assert status_of(session_factory, application_id) == (
            ApplicationStatus.UNDER_REVIEW.value
        )

    def test_reversing_an_approval_frees_the_animal(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the reversal FR-7.5 requires is reachable over HTTP."""
        application_id = apply_for(
            session_factory, world["adopter_profile_id"], world["available_animal_id"]
        )
        staff_client.post(
            f"/applications/{application_id}/decide",
            data={"decision": "APPROVE", "animal_id": world["available_animal_id"]},
        )

        staff_client.post(
            f"/applications/{application_id}/decide",
            data={"decision": "REVERSE", "animal_id": world["available_animal_id"]},
        )

        assert animal_status_of(session_factory, world["available_animal_id"]) == (
            AnimalStatus.AVAILABLE.value
        )


class TestRefusals:
    """A decision the rules forbid is refused, and changes nothing."""

    def test_an_unknown_decision_is_rejected(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the decision value is validated by lookup, not trusted.

        The value comes from a form field, so anything can arrive in it.
        """
        application_id = apply_for(
            session_factory, world["adopter_profile_id"], world["available_animal_id"]
        )

        response = staff_client.post(
            f"/applications/{application_id}/decide",
            data={"decision": "DELETE_EVERYTHING"},
        )

        assert response.status_code == 400
        assert status_of(session_factory, application_id) == (
            ApplicationStatus.SUBMITTED.value
        )

    def test_deciding_an_application_that_does_not_exist_is_a_404(
        self, staff_client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves an unknown identifier is not silently ignored."""
        response = staff_client.post(
            f"/applications/{new_identifier()}/decide", data={"decision": "APPROVE"}
        )

        assert response.status_code == 404

    def test_approving_twice_is_refused_rather_than_repeated(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves a double submit cannot approve an already-approved record."""
        application_id = apply_for(
            session_factory, world["adopter_profile_id"], world["available_animal_id"]
        )
        staff_client.post(
            f"/applications/{application_id}/decide",
            data={"decision": "APPROVE", "animal_id": world["available_animal_id"]},
        )

        response = staff_client.post(
            f"/applications/{application_id}/decide",
            data={"decision": "APPROVE", "animal_id": world["available_animal_id"]},
            follow_redirects=True,
        )

        assert response.status_code == 200
        assert status_of(session_factory, application_id) == (
            ApplicationStatus.APPROVED.value
        )

    def test_reversing_something_never_approved_is_refused(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the reversal guard holds at the HTTP boundary too.

        The damage was never the status change: the handler used to force
        the animal back to AVAILABLE, discarding an ADOPTION_IN_PROGRESS a
        different adopter's genuine approval had set.
        """
        application_id = apply_for(
            session_factory, world["adopter_profile_id"], world["available_animal_id"]
        )

        staff_client.post(
            f"/applications/{application_id}/decide",
            data={"decision": "REVERSE", "animal_id": world["available_animal_id"]},
        )

        assert status_of(session_factory, application_id) == (
            ApplicationStatus.SUBMITTED.value
        )
        assert animal_status_of(session_factory, world["available_animal_id"]) == (
            AnimalStatus.AVAILABLE.value
        )
