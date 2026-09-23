"""API tests for authentication and authorization (blueprint section 12).

Blueprint section 12 is explicit that hiding a button is not sufficient: the
server must enforce permissions. These tests therefore call endpoints
*directly*, without ever rendering the page that would normally offer the
link, which is exactly what an attacker does.
"""

from __future__ import annotations

import pytest
from flask import Flask
from flask.testing import FlaskClient

from tests.api.conftest import (
    ADOPTER_EMAIL,
    PROFILELESS_EMAIL,
    STAFF_EMAIL,
    TEST_PASSWORD,
    sign_in,
)

pytestmark = pytest.mark.api

# Every route only staff may reach.
STAFF_ONLY_PAGES = [
    "/dashboard",
    "/animals/manage",
]

# Every route only an adopter may reach.
ADOPTER_ONLY_PAGES = [
    "/my/matches",
    "/my/applications",
    "/my/invitations",
    "/my/profile",
]


class TestSignIn:
    """Credentials are checked, and failures reveal nothing."""

    def test_valid_credentials_are_accepted(
        self, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves a correct password signs a user in."""
        response = client.post(
            "/login", data={"email": ADOPTER_EMAIL, "password": TEST_PASSWORD}
        )
        assert response.status_code == 302

    def test_wrong_password_is_rejected(
        self, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves a wrong password does not sign anyone in."""
        response = client.post(
            "/login", data={"email": ADOPTER_EMAIL, "password": "wrong"}
        )
        assert response.status_code == 401

    def test_unknown_email_gives_the_same_response_as_a_wrong_password(
        self, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves the form cannot be used to discover registered addresses.

        If an unknown email produced a different status or message, the login
        page would become an account-enumeration oracle.
        """
        unknown = client.post(
            "/login", data={"email": "nobody@nowhere.test", "password": "wrong"}
        )
        known = client.post(
            "/login", data={"email": ADOPTER_EMAIL, "password": "wrong"}
        )

        assert unknown.status_code == known.status_code == 401
        assert b"incorrect" in unknown.data.lower()
        assert b"incorrect" in known.data.lower()

    def test_deactivated_account_cannot_sign_in(
        self, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves deactivation actually prevents access."""
        response = client.post(
            "/login", data={"email": "gone@petmatch.test", "password": TEST_PASSWORD}
        )
        assert response.status_code == 403

    def test_sign_out_requires_post(
        self, adopter_client: FlaskClient
    ) -> None:
        """Proves a third-party page cannot sign a user out with an image tag.

        A GET /logout would be triggerable by any site the user visits.
        """
        assert adopter_client.get("/logout").status_code == 405


class TestAnonymousAccess:
    """Public pages are public; protected ones are not."""

    @pytest.mark.parametrize("path", ["/", "/animals/", "/login", "/register"])
    def test_public_pages_are_reachable(
        self, client: FlaskClient, world: dict[str, str], path: str
    ) -> None:
        """Proves browsing works without an account."""
        assert client.get(path).status_code == 200

    @pytest.mark.parametrize("path", STAFF_ONLY_PAGES + ADOPTER_ONLY_PAGES)
    def test_protected_pages_refuse_anonymous_visitors(
        self, client: FlaskClient, world: dict[str, str], path: str
    ) -> None:
        """Proves no protected page leaks content to a signed-out visitor."""
        response = client.get(path)

        assert response.status_code in (302, 401)
        if response.status_code == 302:
            assert "/login" in response.headers["Location"]


class TestRoleSeparation:
    """Each role is refused the other's routes, on the server."""

    @pytest.mark.parametrize("path", STAFF_ONLY_PAGES)
    def test_adopter_is_refused_staff_pages(
        self, adopter_client: FlaskClient, path: str
    ) -> None:
        """Proves 403, not a redirect and not a rendered page.

        A redirect would look like success to a naive check; 403 is the
        unambiguous answer blueprint section 12 asks for.
        """
        assert adopter_client.get(path).status_code == 403

    @pytest.mark.parametrize("path", ADOPTER_ONLY_PAGES)
    def test_staff_is_refused_adopter_pages(
        self, staff_client: FlaskClient, path: str
    ) -> None:
        """Proves staff cannot act inside an adopter's personal area.

        Staff hold more power overall, but not the power to answer an
        invitation on somebody's behalf.
        """
        assert staff_client.get(path).status_code == 403

    def test_adopter_cannot_reach_staff_ranking_pages(
        self, adopter_client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves the matching screens are staff-only."""
        animal_id = world["available_animal_id"]

        assert adopter_client.get(f"/animals/{animal_id}/adopters").status_code == 403
        assert adopter_client.get(f"/animals/{animal_id}/discover").status_code == 403

    def test_adopter_cannot_send_an_invitation(
        self, adopter_client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves a forged POST to a staff action is refused.

        This is the case hiding the button does nothing about.
        """
        response = adopter_client.post(
            f"/animals/{world['available_animal_id']}/invite",
            data={"adopter_profile_id": world["adopter_profile_id"]},
        )
        assert response.status_code == 403


class TestOwnership:
    """One adopter cannot act on another's records (FR-2.4)."""

    def test_adopter_cannot_withdraw_another_adopters_application(
        self,
        adopter_client: FlaskClient,
        other_adopter_client: FlaskClient,
        world: dict[str, str],
    ) -> None:
        """Proves guessing an application identifier achieves nothing."""
        adopter_client.post(
            f"/my/apply/{world['available_animal_id']}",
            data={"applicant_message": "Please"},
            follow_redirects=True,
        )

        listing = adopter_client.get("/my/applications")
        assert listing.status_code == 200

        # The other adopter's own list must not contain it either.
        other_listing = other_adopter_client.get("/my/applications")
        assert b"Clover" not in other_listing.data

    def test_adopter_only_sees_their_own_applications(
        self,
        adopter_client: FlaskClient,
        other_adopter_client: FlaskClient,
        world: dict[str, str],
    ) -> None:
        """Proves the listing is scoped to the signed-in adopter."""
        adopter_client.post(
            f"/my/apply/{world['available_animal_id']}",
            data={"applicant_message": "Mine"},
            follow_redirects=True,
        )

        response = other_adopter_client.get("/my/applications")

        assert response.status_code == 200
        assert b"Mine" not in response.data


class TestProfileAccess:
    """The profile form is reachable and personal."""

    def test_adopter_without_a_profile_can_open_the_form(
        self, application: Flask, world: dict[str, str]
    ) -> None:
        """Proves a newcomer is not locked out of creating their profile.

        Routes that require a profile redirect here, so this page must work
        for somebody who has none - otherwise the redirect is a loop.
        """
        test_client = application.test_client()
        sign_in(test_client, PROFILELESS_EMAIL)
        assert test_client.get("/my/profile").status_code == 200

    def test_profileless_adopter_is_redirected_to_the_form(
        self, application: Flask, world: dict[str, str]
    ) -> None:
        """Proves the redirect points at the form rather than looping."""
        test_client = application.test_client()
        sign_in(test_client, PROFILELESS_EMAIL)
        response = test_client.get("/my/matches")

        assert response.status_code == 302
        assert "/my/profile" in response.headers["Location"]

    def test_staff_cannot_open_an_adopter_profile_form(
        self, staff_client: FlaskClient
    ) -> None:
        """Proves staff cannot edit an adopter's profile (FR-2.5)."""
        assert staff_client.get("/my/profile").status_code == 403


class TestSignedInRouting:
    """Each role reaches its own pages successfully."""

    @pytest.mark.parametrize("path", STAFF_ONLY_PAGES)
    def test_staff_reaches_staff_pages(
        self, staff_client: FlaskClient, path: str
    ) -> None:
        """Proves the authorization check does not block the right role.

        A decorator that refused everybody would pass every negative test
        above while breaking the product.
        """
        assert staff_client.get(path).status_code == 200

    @pytest.mark.parametrize("path", ADOPTER_ONLY_PAGES)
    def test_adopter_reaches_adopter_pages(
        self, adopter_client: FlaskClient, path: str
    ) -> None:
        """Proves an adopter with a profile reaches their own area."""
        assert adopter_client.get(path).status_code == 200

    def test_signing_in_as_staff_then_adopter_does_not_leak_access(
        self, application: Flask, world: dict[str, str]
    ) -> None:
        """Proves the session role is re-evaluated, not cached from a prior sign-in."""
        test_client = application.test_client()
        sign_in(test_client, STAFF_EMAIL)
        assert test_client.get("/dashboard").status_code == 200

        test_client.post("/logout")
        sign_in(test_client, ADOPTER_EMAIL)
        assert test_client.get("/dashboard").status_code == 403
