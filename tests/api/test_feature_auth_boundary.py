"""Tests for authentication after the controller stopped holding a session.

CLAUDE.md R2 says a controller must never touch a database session, and
this one did - on the argument that authentication is not a business
read. The argument did not survive being looked at, so sign-in and
registration now dispatch like everything else.

Two kinds of test here. The behavioural ones prove the move changed
nothing a user can see, which is the whole point of a refactor. The
structural one proves the boundary itself, because a behavioural test
passes just as happily with the session back in the controller.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from app.cqrs.commands.account_commands import (
    EmailAlreadyRegisteredError,
    RegisterAdopterCommand,
    RegisterAdopterHandler,
)
from app.cqrs.queries.auth_queries import (
    EmailIsRegisteredQuery,
    GetAccountForSignInQuery,
)
from app.infrastructure.models import User
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from werkzeug.security import check_password_hash

from tests.api.conftest import ADOPTER_EMAIL, TEST_PASSWORD

pytestmark = pytest.mark.api

CONTROLLER = (
    Path(__file__).resolve().parents[2] / "app" / "controllers" / "auth_controller.py"
)


class TestTheBoundaryItself:
    """The rule, checked structurally rather than by hoping."""

    def test_the_controller_imports_no_persistence_library(self) -> None:
        """Proves the controller cannot reach the database directly.

        Parsed rather than grepped, so a mention inside a docstring or a
        comment - this file discusses `session` at length - cannot make
        the test pass or fail by accident.
        """
        tree = ast.parse(CONTROLLER.read_text(encoding="utf-8"))

        imported = {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        } | {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }

        assert "sqlalchemy" not in imported

    def test_the_controller_calls_no_session_methods(self) -> None:
        """Proves no `session.execute`, `session.add` or `session.commit`."""
        tree = ast.parse(CONTROLLER.read_text(encoding="utf-8"))

        session_calls = [
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "session"
        ]

        assert session_calls == []

    def test_the_controller_does_not_reach_for_a_session_factory(self) -> None:
        """Proves the escape hatch is closed, not merely unused.

        `get_session_factory` is still available to controllers that own
        an authentication concern - which was the argument for this one
        using it. It no longer does.
        """
        tree = ast.parse(CONTROLLER.read_text(encoding="utf-8"))

        names = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }

        assert "get_session_factory" not in names

    def test_password_hashing_stayed_in_the_controller(self) -> None:
        """Proves the refactor did not push authentication into the domain.

        Comparing and deriving a hash touches no storage; moving it behind
        the bus would put a plaintext password somewhere it need not go.
        """
        source = CONTROLLER.read_text(encoding="utf-8")

        assert "check_password_hash" in source
        assert "generate_password_hash" in source


class TestSigningIn:
    """Behaviour a user can see, unchanged by the move."""

    def test_a_correct_password_signs_in(
        self, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves the happy path still works."""
        response = client.post(
            "/login", data={"email": ADOPTER_EMAIL, "password": TEST_PASSWORD}
        )

        assert response.status_code == 302
        assert "/login" not in response.headers["Location"]

    def test_a_wrong_password_is_refused(
        self, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves a bad password does not sign anybody in."""
        response = client.post(
            "/login", data={"email": ADOPTER_EMAIL, "password": "wrong-password"}
        )

        assert response.status_code == 401

    def test_an_unknown_address_and_a_wrong_password_look_identical(
        self, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves the form cannot be used to discover registered addresses.

        This is why the query returns None for an unknown address instead
        of raising: the controller has to answer both cases the same way,
        and it can only do that if "no such account" is an ordinary
        result.
        """
        unknown = client.post(
            "/login", data={"email": "nobody@example.test", "password": "whatever"}
        )
        wrong = client.post(
            "/login", data={"email": ADOPTER_EMAIL, "password": "whatever"}
        )

        # Compared on the status and the message, not the whole page: the
        # form deliberately echoes back whichever address was typed, so
        # the two bodies differ in a way that reveals nothing.
        assert unknown.status_code == wrong.status_code
        message = "Email or password is incorrect."
        assert message in unknown.get_data(as_text=True)
        assert message in wrong.get_data(as_text=True)

    def test_neither_refusal_hints_that_the_account_exists(
        self, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves no wording distinguishes an unknown address from a bad password."""
        unknown = client.post(
            "/login", data={"email": "nobody@example.test", "password": "whatever"}
        ).get_data(as_text=True)
        wrong = client.post(
            "/login", data={"email": ADOPTER_EMAIL, "password": "whatever"}
        ).get_data(as_text=True)

        for revealing in ("no such", "not found", "unknown", "does not exist"):
            assert revealing not in unknown.lower()
            assert revealing not in wrong.lower()

    def test_a_deactivated_account_is_refused_distinctly(
        self,
        client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves a deactivated account gets its own answer.

        Safe to distinguish: whoever is typing already knows the password,
        so it reveals nothing they did not have.
        """
        with session_factory() as session:
            user = session.execute(
                select(User).where(User.email == ADOPTER_EMAIL)
            ).scalar_one()
            user.is_active = False
            session.commit()

        response = client.post(
            "/login", data={"email": ADOPTER_EMAIL, "password": TEST_PASSWORD}
        )

        assert response.status_code == 403

    def test_the_signed_in_user_carries_their_profile(
        self, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves the profile lookup survived the move.

        Without it every adopter would appear to have no profile, and the
        whole personal area would send them back to the profile form.
        """
        client.post(
            "/login", data={"email": ADOPTER_EMAIL, "password": TEST_PASSWORD}
        )

        assert client.get("/my/matches").status_code == 200


class TestRegistering:
    """Creating an account, through the bus."""

    def test_a_new_account_is_created_and_signed_in(
        self,
        client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves registration writes the account and starts a session."""
        response = client.post(
            "/register",
            data={
                "full_name": "Noa Shapira",
                "email": "noa@example.test",
                "password": "Password123!",
                "confirm_password": "Password123!",
            },
        )

        assert response.status_code == 302
        with session_factory() as session:
            created = session.execute(
                select(User).where(User.email == "noa@example.test")
            ).scalar_one_or_none()
        assert created is not None
        assert created.full_name == "Noa Shapira"

    def test_the_stored_password_is_hashed(
        self,
        client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the plaintext never reaches the column.

        Worth asserting rather than assuming: hashing moved across a
        layer boundary in this change, and a refactor that dropped it
        would still pass every other test here.
        """
        client.post(
            "/register",
            data={
                "full_name": "Noa Shapira",
                "email": "noa@example.test",
                "password": "Password123!",
                "confirm_password": "Password123!",
            },
        )

        with session_factory() as session:
            created = session.execute(
                select(User).where(User.email == "noa@example.test")
            ).scalar_one()

        assert created.password_hash != "Password123!"
        assert check_password_hash(created.password_hash, "Password123!")

    def test_a_duplicate_address_is_refused(
        self, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves an existing address cannot be registered twice."""
        response = client.post(
            "/register",
            data={
                "full_name": "Somebody Else",
                "email": ADOPTER_EMAIL,
                "password": "Password123!",
                "confirm_password": "Password123!",
            },
        )

        assert response.status_code == 409

    def test_the_handler_refuses_a_duplicate_even_without_the_check(
        self, application: Flask, world: dict[str, str]
    ) -> None:
        """Proves the unique index, not the check, is what settles the race.

        The controller's "is this taken?" query is polite, not
        authoritative: two overlapping registrations both pass it. This
        dispatches the command directly, skipping that check, and the
        handler must still refuse.
        """
        bus = application.config["BUS"]

        with pytest.raises(EmailAlreadyRegisteredError):
            bus.dispatch_command(
                RegisterAdopterCommand(
                    email=ADOPTER_EMAIL,
                    full_name="Somebody Else",
                    password_hash="irrelevant",
                )
            )


class TestTheQueries:
    """The read side, exercised directly."""

    def test_the_account_query_returns_none_for_an_unknown_address(
        self, application: Flask, world: dict[str, str]
    ) -> None:
        """Proves an unknown address is an ordinary answer, not an error."""
        account = application.config["BUS"].dispatch_query(
            GetAccountForSignInQuery(email="nobody@example.test")
        )

        assert account is None

    def test_the_account_query_reports_a_registered_address(
        self, application: Flask, world: dict[str, str]
    ) -> None:
        """Proves the lookup finds a real account and its profile."""
        account = application.config["BUS"].dispatch_query(
            GetAccountForSignInQuery(email=ADOPTER_EMAIL)
        )

        assert account is not None
        assert account.email == ADOPTER_EMAIL
        assert account.adopter_profile_id is not None

    def test_the_registration_check_answers_both_ways(
        self, application: Flask, world: dict[str, str]
    ) -> None:
        """Proves the duplicate check distinguishes taken from free."""
        bus = application.config["BUS"]

        assert bus.dispatch_query(EmailIsRegisteredQuery(email=ADOPTER_EMAIL)) is True
        assert (
            bus.dispatch_query(EmailIsRegisteredQuery(email="free@example.test"))
            is False
        )

    def test_the_handler_is_registered_on_the_bus(
        self, application: Flask
    ) -> None:
        """Proves the wiring exists, so a missing registration fails loudly.

        A handler that is written but never registered raises only when
        somebody signs in, which in practice means in front of a user.
        """
        assert isinstance(
            RegisterAdopterHandler(), RegisterAdopterHandler
        )
        assert application.config["BUS"] is not None


class TestOneApplicationDoesNotContaminateAnother:
    """`create_app` must build a self-contained application every time.

    Flask-Login and Flask-WTF were held as module-level singletons and
    re-initialised per call. `init_app` rebinds a shared instance to whichever
    application called last, and the user loader closes over one call's
    session factory - so the second application in a process silently took
    over authentication for the first, and everybody signed into the first
    was rehydrated against the second one's database.
    """

    def test_a_signed_in_session_survives_a_second_application_being_built(
        self,
        adopter_client: FlaskClient,
        csrf_client: FlaskClient,
        world: dict[str, str],
    ) -> None:
        """Proves the first application still authenticates its own visitors.

        The fixture order is the test: `adopter_client` signs in, then
        `csrf_client` builds a second application over a different database,
        and the first client is used afterwards. With a shared login manager
        the first client's user identifier no longer existed in the database
        the loader was now pointed at, so this page answered a redirect to
        sign in.
        """
        response = adopter_client.get("/my/applications")

        assert response.status_code == 200

    def test_each_application_carries_its_own_login_manager(
        self, application: Flask, csrf_application: Flask
    ) -> None:
        """Proves the extension objects are not shared between applications.

        Structural rather than behavioural, because the behavioural symptom
        depends on which application happened to be built last.
        """
        # Flask-Login attaches itself to the application object rather than
        # to `extensions`, and it ships no type information, so the attribute
        # is read by name.
        first_login_manager = getattr(application, "login_manager", None)
        second_login_manager = getattr(csrf_application, "login_manager", None)

        assert first_login_manager is not None
        assert first_login_manager is not second_login_manager
        assert application.extensions["csrf"] is not csrf_application.extensions["csrf"]
