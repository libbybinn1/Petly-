"""Shared fixtures for the API suite.

These tests drive the real Flask application through its HTTP surface, using
an in-memory SQLite database rather than the cloud one. The thing under test
is routing, authentication, authorization and validation - none of which
depends on the SQL dialect, and all of which would be painfully slow against
a shared free-tier server.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from app import create_app
from app.config import AgentConfiguration, Configuration, DatabaseConfiguration
from app.domain.enums import (
    ActivityLevel,
    AnimalSize,
    AnimalStatus,
    ExperienceLevel,
    HomeType,
    Species,
    Temperament,
    UserRole,
)
from app.infrastructure.database import Base
from app.infrastructure.models import (
    AdopterProfile,
    Animal,
    AnimalImage,
    User,
    new_identifier,
)
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from werkzeug.security import generate_password_hash

TEST_PASSWORD = "Password123!"

STAFF_EMAIL = "staff@petmatch.test"
ADOPTER_EMAIL = "adopter@petmatch.test"
OTHER_ADOPTER_EMAIL = "other@petmatch.test"
PROFILELESS_EMAIL = "newcomer@petmatch.test"


def _now() -> datetime:
    """Naive UTC, matching how the application stores timestamps."""
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture
def configuration(tmp_path: Path) -> Configuration:
    """Application configuration pointed at a throwaway database.

    The agent settings are present but unused: no API test touches a model,
    because an HTTP test that waited on inference would be unusably slow and
    would be testing the wrong thing.
    """
    return Configuration(
        database=DatabaseConfiguration(
            server="unused", database="unused", user="unused", password="unused"
        ),
        agent=AgentConfiguration(
            ollama_base_url="http://localhost:11434",
            chat_model="stub",
            fast_chat_model="stub",
            embedding_model="stub",
            tavily_api_key="",
            poll_interval_seconds=3,
            max_reasoning_steps=8,
        ),
        secret_key="test-secret-key",
        flask_port=5000,
        is_development=False,
        chroma_persist_directory=tmp_path / "chroma",
        rag_collection_name="test",
        upload_directory=tmp_path / "uploads",
        invitation_expiry_hours=72,
    )


@pytest.fixture
def application(configuration: Configuration, monkeypatch: pytest.MonkeyPatch) -> Flask:
    """The real Flask app, wired to a temporary SQLite database."""
    database_url = f"sqlite:///{configuration.chroma_persist_directory.parent}/api.db"
    monkeypatch.setenv("LOCAL_DATABASE_URL", database_url)

    engine = create_engine(database_url, future=True)
    Base.metadata.create_all(engine)
    engine.dispose()

    flask_application = create_app(replace(configuration))
    flask_application.config["TESTING"] = True
    # Exceptions must surface as 500s rather than being re-raised, so a test
    # asserting on a status code sees what a browser would.
    flask_application.config["PROPAGATE_EXCEPTIONS"] = False
    return flask_application


@pytest.fixture
def session_factory(application: Flask) -> sessionmaker[Session]:
    """The session factory the application itself is using."""
    return application.config["SESSION_FACTORY"]


@pytest.fixture
def world(session_factory: sessionmaker[Session]) -> dict[str, str]:
    """A staff member, two adopters with profiles, one without, and two animals."""
    identifiers: dict[str, str] = {}
    password_hash = generate_password_hash(TEST_PASSWORD)

    with session_factory() as session:
        staff = User(
            user_id=new_identifier(), email=STAFF_EMAIL, password_hash=password_hash,
            full_name="Staff Member", role=UserRole.STAFF.value,
            is_active=True, created_at=_now(),
        )
        newcomer = User(
            user_id=new_identifier(), email=PROFILELESS_EMAIL,
            password_hash=password_hash, full_name="New Comer",
            role=UserRole.ADOPTER.value, is_active=True, created_at=_now(),
        )
        deactivated = User(
            user_id=new_identifier(), email="gone@petmatch.test",
            password_hash=password_hash, full_name="Gone Away",
            role=UserRole.ADOPTER.value, is_active=False, created_at=_now(),
        )
        session.add_all([staff, newcomer, deactivated])

        for label, email in (("adopter", ADOPTER_EMAIL), ("other", OTHER_ADOPTER_EMAIL)):
            user = User(
                user_id=new_identifier(), email=email, password_hash=password_hash,
                full_name=label.title(), role=UserRole.ADOPTER.value,
                is_active=True, created_at=_now(),
            )
            profile = AdopterProfile(
                adopter_profile_id=new_identifier(), user_id=user.user_id,
                home_type=HomeType.HOUSE.value, has_yard=True,
                household_has_children=False, has_other_animals=False,
                experience_level=ExperienceLevel.SOME.value,
                activity_level=ActivityLevel.MODERATE.value,
                daily_hours_available=4.0, city="Haifa",
                open_to_proactive_suggestions=True, is_complete=True,
                created_at=_now(), updated_at=_now(),
            )
            session.add_all([user, profile])
            identifiers[f"{label}_user_id"] = user.user_id
            identifiers[f"{label}_profile_id"] = profile.adopter_profile_id

        available = Animal(
            animal_id=new_identifier(), name="Clover", species=Species.RABBIT.value,
            age_years=2.0, size=AnimalSize.SMALL.value,
            temperament=Temperament.CALM.value, activity_level=ActivityLevel.LOW.value,
            good_with_children=True, good_with_other_animals=True,
            has_special_needs=False, required_space=AnimalSize.SMALL.value,
            city="Haifa", status=AnimalStatus.AVAILABLE.value,
            description="A calm rabbit.", created_at=_now(), updated_at=_now(),
        )
        adopted = Animal(
            animal_id=new_identifier(), name="Gus", species=Species.DOG.value,
            age_years=8.0, size=AnimalSize.MEDIUM.value,
            temperament=Temperament.CALM.value, activity_level=ActivityLevel.LOW.value,
            good_with_children=True, good_with_other_animals=True,
            has_special_needs=False, required_space=AnimalSize.MEDIUM.value,
            city="Haifa", status=AnimalStatus.ADOPTED.value,
            created_at=_now(), updated_at=_now(),
        )
        session.add_all([available, adopted])
        session.flush()

        session.add(
            AnimalImage(
                animal_image_id=new_identifier(), animal_id=available.animal_id,
                image_url="/static/uploads/test.jpg", is_primary=True,
                display_order=0, uploaded_at=_now(),
            )
        )

        session.commit()
        identifiers.update(
            staff_user_id=staff.user_id,
            newcomer_user_id=newcomer.user_id,
            available_animal_id=available.animal_id,
            adopted_animal_id=adopted.animal_id,
        )

    return identifiers


@pytest.fixture
def client(application: Flask) -> FlaskClient:
    """An anonymous HTTP client.

    Returned rather than yielded from a `with` block. Two clients active in
    one test would otherwise nest application contexts and unwind in the
    wrong order, which Flask reports as "Popped wrong app context".
    """
    return application.test_client()


def sign_in(test_client: FlaskClient, email: str) -> None:
    """Sign a client in, asserting it worked.

    Asserting here means a broken login surfaces as a failure in the test
    that depends on it, rather than as a confusing 401 later.
    """
    response = test_client.post(
        "/login", data={"email": email, "password": TEST_PASSWORD}
    )
    assert response.status_code in (302, 200), f"sign-in failed for {email}"


@pytest.fixture
def staff_client(application: Flask, world: dict[str, str]) -> FlaskClient:
    """A client signed in as staff."""
    test_client = application.test_client()
    sign_in(test_client, STAFF_EMAIL)
    return test_client


@pytest.fixture
def adopter_client(application: Flask, world: dict[str, str]) -> FlaskClient:
    """A client signed in as an adopter with a complete profile."""
    test_client = application.test_client()
    sign_in(test_client, ADOPTER_EMAIL)
    return test_client


@pytest.fixture
def other_adopter_client(application: Flask, world: dict[str, str]) -> FlaskClient:
    """A second adopter, for proving one cannot reach the other's records."""
    test_client = application.test_client()
    sign_in(test_client, OTHER_ADOPTER_EMAIL)
    return test_client
