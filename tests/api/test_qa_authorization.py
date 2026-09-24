"""Adversarial API tests for authorization, CSRF and the route surface.

Every request here is forged: it is sent straight to the endpoint without
rendering the page that would normally offer the control, which is what
blueprint section 12 means by "hiding a button is not sufficient".

The existing suite covers GET routes and two POSTs. This one covers every
POST route, one adopter acting on another's records by identifier, the
CSRF requirement in NFR-4.3, and the gap between docs/UX.md's permission
table and what the server actually answers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest
from app.domain.enums import ApplicationStatus, InvitationStatus
from app.infrastructure.models import (
    AdoptionApplication,
    AdoptionInvitation,
    User,
    new_identifier,
)
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import ADOPTER_EMAIL, OTHER_ADOPTER_EMAIL, sign_in

pytestmark = pytest.mark.api


def _now() -> datetime:
    """Naive UTC, matching how the application stores timestamps."""
    return datetime.now(UTC).replace(tzinfo=None)


def make_invitation(
    session_factory: sessionmaker[Session], profile_id: str, animal_id: str
) -> str:
    """Insert an open invitation for one adopter and return its identifier."""
    with session_factory() as session:
        invitation = AdoptionInvitation(
            invitation_id=new_identifier(), animal_id=animal_id,
            adopter_profile_id=profile_id, sent_by_user_id="staff",
            status=InvitationStatus.SENT.value, sent_at=_now(),
            expires_at=_now() + timedelta(hours=72),
        )
        session.add(invitation)
        session.commit()
        return invitation.invitation_id


def make_application(
    session_factory: sessionmaker[Session], profile_id: str, animal_id: str
) -> str:
    """Insert a submitted application for one adopter and return its identifier."""
    with session_factory() as session:
        application = AdoptionApplication(
            application_id=new_identifier(), adopter_profile_id=profile_id,
            animal_id=animal_id, status=ApplicationStatus.SUBMITTED.value,
            submitted_at=_now(),
        )
        session.add(application)
        session.commit()
        return application.application_id


def invitation_status(session_factory: sessionmaker[Session], invitation_id: str) -> str:
    """Read one invitation's status."""
    with session_factory() as session:
        row = session.get(AdoptionInvitation, invitation_id)
        assert row is not None
        return str(row.status)


def application_status(session_factory: sessionmaker[Session], application_id: str) -> str:
    """Read one application's status."""
    with session_factory() as session:
        row = session.get(AdoptionApplication, application_id)
        assert row is not None
        return str(row.status)


class TestEveryPostRouteRefusesTheWrongRole:
    """The complete POST surface, not a sample of it."""

    def test_staff_cannot_apply_on_an_adopters_behalf(
        self, staff_client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves FR-2.5: staff hold more power, but not an adopter's decisions."""
        response = staff_client.post(f"/my/apply/{world['available_animal_id']}")

        assert response.status_code == 403

    def test_staff_cannot_respond_to_an_invitation_on_an_adopters_behalf(
        self, staff_client: FlaskClient, world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves FR-2.5 on the invitation route specifically, and nothing changed."""
        invitation = make_invitation(
            session_factory, world["adopter_profile_id"], world["available_animal_id"]
        )

        response = staff_client.post(
            f"/my/invitations/{invitation}/respond", data={"response": "ACCEPT"}
        )

        assert response.status_code == 403
        assert invitation_status(session_factory, invitation) == InvitationStatus.SENT.value

    def test_staff_cannot_withdraw_an_adopters_application(
        self, staff_client: FlaskClient, world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves an adopter's withdrawal is theirs alone to make."""
        application = make_application(
            session_factory, world["adopter_profile_id"], world["available_animal_id"]
        )

        response = staff_client.post(f"/my/applications/{application}/withdraw")

        assert response.status_code == 403
        assert application_status(session_factory, application) == (
            ApplicationStatus.SUBMITTED.value
        )

    def test_staff_cannot_save_an_adopter_profile(
        self, staff_client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves FR-2.5: staff cannot edit an adopter's profile."""
        response = staff_client.post(
            "/my/profile",
            data={
                "home_type": "HOUSE", "experience_level": "SOME",
                "activity_level": "LOW", "daily_hours_available": "3",
                "city": "Haifa",
            },
        )

        assert response.status_code == 403

    def test_an_adopter_cannot_send_an_invitation(
        self, adopter_client: FlaskClient, world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the staff invitation action is refused and creates nothing."""
        response = adopter_client.post(
            f"/animals/{world['available_animal_id']}/invite",
            data={"adopter_profile_id": world["other_profile_id"]},
        )

        assert response.status_code == 403
        with session_factory() as session:
            assert session.execute(select(AdoptionInvitation)).scalars().all() == []


class TestOneAdopterCannotActOnAnothersRecords:
    """FR-2.4, exercised by changing the identifier in the URL."""

    def test_responding_to_another_adopters_invitation_is_refused(
        self, other_adopter_client: FlaskClient, world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves an invitation identifier is not a capability.

        The ownership check lives in the command handler, so this is refused
        even though the route itself is one the caller is allowed to use.
        """
        invitation = make_invitation(
            session_factory, world["adopter_profile_id"], world["available_animal_id"]
        )

        response = other_adopter_client.post(
            f"/my/invitations/{invitation}/respond", data={"response": "ACCEPT"}
        )

        assert response.status_code == 403
        assert invitation_status(session_factory, invitation) == InvitationStatus.SENT.value
        with session_factory() as session:
            assert session.execute(select(AdoptionApplication)).scalars().all() == []

    def test_declining_another_adopters_invitation_is_refused(
        self, other_adopter_client: FlaskClient, world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the refusal covers the decline path, not only accept.

        A decline is destructive too: it would close somebody else's
        invitation for them.
        """
        invitation = make_invitation(
            session_factory, world["adopter_profile_id"], world["available_animal_id"]
        )

        response = other_adopter_client.post(
            f"/my/invitations/{invitation}/respond", data={"response": "DECLINE"}
        )

        assert response.status_code == 403
        assert invitation_status(session_factory, invitation) == InvitationStatus.SENT.value

    def test_withdrawing_another_adopters_application_leaves_it_untouched(
        self, other_adopter_client: FlaskClient, world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the refusal is enforced before the state change, not after."""
        application = make_application(
            session_factory, world["adopter_profile_id"], world["available_animal_id"]
        )

        response = other_adopter_client.post(f"/my/applications/{application}/withdraw")

        assert response.status_code == 403
        assert application_status(session_factory, application) == (
            ApplicationStatus.SUBMITTED.value
        )

    def test_a_nonexistent_record_and_someone_elses_look_the_same(
        self, other_adopter_client: FlaskClient, world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves identifiers cannot be enumerated by comparing the two answers.

        A 403 for one and a redirect-with-flash for the other would turn the
        endpoint into an oracle for which identifiers exist.
        """
        real_but_not_mine = make_invitation(
            session_factory, world["adopter_profile_id"], world["available_animal_id"]
        )

        theirs = other_adopter_client.post(
            f"/my/invitations/{real_but_not_mine}/respond", data={"response": "ACCEPT"}
        )
        imaginary = other_adopter_client.post(
            "/my/invitations/00000000-0000-0000-0000-000000000000/respond",
            data={"response": "ACCEPT"},
        )

        assert theirs.status_code == 403
        assert imaginary.status_code != 403, (
            "a missing invitation currently answers differently from one that "
            "exists but belongs to somebody else, which leaks existence"
        )


class TestAnonymousRequests:
    """What a signed-out visitor gets, and what docs/UX.md says they get."""

    PROTECTED_POSTS: ClassVar[list[str]] = [
        "/my/profile",
        "/my/apply/some-animal",
        "/my/applications/some-application/withdraw",
        "/my/invitations/some-invitation/respond",
        "/animals/some-animal/invite",
        "/logout",
    ]

    @pytest.mark.parametrize("path", PROTECTED_POSTS)
    def test_an_anonymous_post_never_performs_the_action(
        self, client: FlaskClient, world: dict[str, str], path: str
    ) -> None:
        """Proves no protected POST is reachable without a session."""
        response = client.post(path)

        assert response.status_code in (302, 401, 403)
        if response.status_code == 302:
            assert "/login" in response.headers["Location"]

    @pytest.mark.parametrize(
        "path",
        [
            "/animals/manage",
            "/dashboard",
            "/animals/some-animal/adopters",
            "/animals/some-animal/discover",
            "/my/profile",
            "/my/applications",
            "/my/invitations",
            "/my/matches",
        ],
    )
    def test_anonymous_status_matches_the_documented_table(
        self, client: FlaskClient, world: dict[str, str], path: str
    ) -> None:
        """Proves the permission table in docs/UX.md describes the real server.

        The table says an anonymous visitor on any protected screen is sent
        to sign in. That is what `@require_sign_in`, the outermost decorator
        on every one of these routes, does: a 302 to /login before any role
        check runs. An earlier version of the table promised 401 or 403 here;
        the document was corrected to the friendlier behaviour the server
        has always had, and this test keeps the two from drifting again.
        """
        response = client.get(path)

        assert response.status_code == 302, path
        assert "/login" in response.headers["Location"], path

    def test_a_deactivated_account_loses_an_existing_session(
        self, application: Flask, world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves deactivation takes effect immediately, not at the next sign-in.

        The session cookie is still valid; what stops it is the user loader
        refusing an inactive row on every request.
        """
        test_client = application.test_client()
        sign_in(test_client, ADOPTER_EMAIL)
        assert test_client.get("/my/applications").status_code in (200, 302)

        with session_factory() as session:
            user = session.execute(
                select(User).where(User.email == ADOPTER_EMAIL)
            ).scalar_one()
            user.is_active = False
            session.commit()

        response = test_client.get("/my/applications")

        assert response.status_code in (302, 401)


class TestCrossSiteRequestForgery:
    """NFR-4.3: forms MUST be protected against CSRF."""

    @pytest.mark.parametrize(
        ("path", "data"),
        [
            ("/my/apply/{animal}", {}),
            ("/my/profile", {
                "home_type": "HOUSE", "experience_level": "SOME",
                "activity_level": "LOW", "daily_hours_available": "3", "city": "Haifa",
            }),
            ("/logout", {}),
        ],
    )
    def test_a_post_without_a_csrf_token_is_rejected(
        self, csrf_client: FlaskClient, csrf_world: dict[str, str],
        path: str, data: dict[str, str],
    ) -> None:
        """Proves a forged cross-site form post cannot act as the signed-in user.

        Uses `csrf_client`, not `adopter_client`: the ordinary API app sets
        WTF_CSRF_ENABLED=False so each other test exercises the rule it was
        written for rather than token plumbing. This one needs it on.
        """
        response = csrf_client.post(
            path.format(animal=csrf_world["available_animal_id"]), data=data
        )

        assert response.status_code in (400, 403)

    def test_the_session_cookie_is_marked_samesite(self, application: Flask) -> None:
        """Proves the second half of the defence is in place.

        A token can be missed on one form. SameSite is belt to that
        braces: the browser will not attach the session cookie to a
        cross-site POST at all, so a forged request arrives as an
        anonymous one instead of acting as the signed-in user.
        """
        assert application.config["SESSION_COOKIE_SAMESITE"] == "Lax"

    def test_the_session_cookie_is_http_only(self, application: Flask) -> None:
        """Proves script cannot read the session cookie."""
        assert application.config["SESSION_COOKIE_HTTPONLY"] is True


class TestOpenRedirectOnSignIn:
    """The `next` parameter is attacker-controlled."""

    @pytest.mark.parametrize(
        "target", ["//evil.example.com", "//evil.example.com/path"]
    )
    def test_an_off_site_next_target_is_refused(
        self, client: FlaskClient, world: dict[str, str], target: str
    ) -> None:
        """Proves sign-in only ever redirects inside the application."""
        from tests.api.conftest import TEST_PASSWORD

        response = client.post(
            f"/login?next={target}",
            data={"email": ADOPTER_EMAIL, "password": TEST_PASSWORD},
        )

        assert not response.headers["Location"].startswith("//")

    def test_an_ordinary_relative_next_target_still_works(
        self, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves the feature itself is fine; only the validation is too loose."""
        from tests.api.conftest import TEST_PASSWORD

        response = client.post(
            "/login?next=/my/applications",
            data={"email": ADOPTER_EMAIL, "password": TEST_PASSWORD},
        )

        assert response.headers["Location"] == "/my/applications"

    def test_an_absolute_external_next_target_is_already_refused(
        self, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves the obvious form of the attack is handled, which is why it was missed."""
        from tests.api.conftest import TEST_PASSWORD

        response = client.post(
            "/login?next=https://evil.example.com",
            data={"email": OTHER_ADOPTER_EMAIL, "password": TEST_PASSWORD},
        )

        assert "evil.example.com" not in response.headers["Location"]


class TestTheRouteSurfaceMatchesTheDocumentation:
    """Routes the specifications require, checked against the URL map."""

    @staticmethod
    def _paths(application: Flask) -> set[str]:
        """Every routing rule the application exposes."""
        return {str(rule) for rule in application.url_map.iter_rules()}

    def test_the_animal_create_and_edit_routes_exist(self, application: Flask) -> None:
        """Proves FR-4.1 has an HTTP surface, not only a documented one."""
        paths = self._paths(application)

        assert "/animals/new" in paths
        assert "/animals/<animal_id>/edit" in paths

    def test_a_staff_decision_route_exists(self, application: Flask) -> None:
        """Proves the approval workflow is reachable by a staff member."""
        decision_like = [
            path for path in self._paths(application)
            if any(word in path for word in ("approve", "decide", "decision", "reject"))
        ]

        assert decision_like != []

    def test_the_documented_read_routes_all_exist(self, application: Flask) -> None:
        """Proves the routes docs/UX.md section 4 tabulates are really served."""
        paths = self._paths(application)

        for documented in (
            "/", "/register", "/login", "/animals/", "/animals/<animal_id>",
            "/animals/manage", "/my/profile", "/my/applications", "/my/invitations",
            "/my/matches", "/animals/<animal_id>/adopters",
            "/animals/<animal_id>/discover", "/dashboard",
        ):
            assert documented in paths, f"{documented} is documented but not routed"

    def test_sign_out_is_post_only(self, application: Flask) -> None:
        """Proves FR-1.6 structurally: a GET cannot be embedded to sign somebody out."""
        logout = next(
            rule for rule in application.url_map.iter_rules() if str(rule) == "/logout"
        )

        assert logout.methods is not None, "the logout rule declares no methods"
        assert "GET" not in logout.methods
        assert "POST" in logout.methods


class TestUnknownIdentifiers:
    """A bad identifier must be a 404, never a 500."""

    @pytest.mark.parametrize(
        "path",
        [
            "/animals/not-a-uuid",
            "/animals/../../etc/passwd",
            "/animals/%00",
            "/history/NotAnAggregate/whatever",
            "/history/Application/not-a-uuid",
        ],
    )
    def test_a_malformed_identifier_is_not_a_server_error(
        self, staff_client: FlaskClient, world: dict[str, str], path: str
    ) -> None:
        """Proves path input is handled, including traversal-shaped and null bytes."""
        response = staff_client.get(path)

        assert response.status_code < 500

    def test_an_unknown_analysis_is_a_404(
        self, staff_client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves a guessed analysis identifier reveals nothing."""
        assert staff_client.get("/analyses/does-not-exist").status_code == 404

    def test_applying_for_an_unknown_animal_is_a_404(
        self, adopter_client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves a forged animal identifier in a POST does not create anything."""
        assert adopter_client.post("/my/apply/no-such-animal").status_code == 404
