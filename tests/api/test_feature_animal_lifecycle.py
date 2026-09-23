"""HTTP tests for creating, editing and retiring animals (FR-4.1, 4.2, 4.4).

Before these routes existed the seed script was the only way an animal
entered the system, so three MUST requirements had no surface at all. These
tests drive them the way a staff member does, with authorization enforced on
the server rather than by hiding a button (blueprint section 12).
"""

from __future__ import annotations

import pytest
from app.domain.enums import AnimalStatus
from app.infrastructure.models import Animal, AnimalImage, new_identifier
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.api


def animal_form(**overrides: object) -> dict[str, object]:
    """A complete, valid animal form submission."""
    form: dict[str, object] = {
        "name": "Pepper",
        "species": "DOG",
        "breed": "Collie cross",
        "age_years": "4",
        "size": "MEDIUM",
        "temperament": "BALANCED",
        "activity_level": "MODERATE",
        "required_space": "MEDIUM",
        "city": "Haifa",
        "status": "AVAILABLE",
        "description": "Gentle and used to a busy household.",
        "good_with_children": "1",
        "good_with_other_animals": "1",
        "image_urls": ["/static/uploads/pepper.jpg"],
    }
    form.update(overrides)
    return form


def animal_named(
    session_factory: sessionmaker[Session], name: str
) -> Animal | None:
    """Read back one animal by name."""
    with session_factory() as session:
        return session.execute(
            select(Animal).where(Animal.name == name)
        ).scalar_one_or_none()


def image_count(session_factory: sessionmaker[Session], animal_id: str) -> int:
    """How many photographs an animal has."""
    with session_factory() as session:
        return len(
            session.execute(
                select(AnimalImage).where(AnimalImage.animal_id == animal_id)
            ).scalars().all()
        )


class TestAuthorization:
    """Listing and editing animals is staff work."""

    @pytest.mark.parametrize("path", ["/animals/new", "/animals/manage"])
    def test_an_adopter_cannot_open_a_staff_screen(
        self, adopter_client: FlaskClient, world: dict[str, str], path: str
    ) -> None:
        """Proves the server refuses, rather than the navigation omitting a link."""
        assert adopter_client.get(path).status_code == 403

    def test_an_adopter_cannot_create_an_animal(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves a forged post from an adopter does not list an animal."""
        response = adopter_client.post("/animals/new", data=animal_form())

        assert response.status_code == 403
        assert animal_named(session_factory, "Pepper") is None

    def test_an_adopter_cannot_change_a_status(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves FR-4.4 is staff-only on the server."""
        response = adopter_client.post(
            f"/animals/{world['available_animal_id']}/status",
            data={"status": "ADOPTED"},
        )

        assert response.status_code == 403

    def test_an_anonymous_visitor_is_sent_to_sign_in(self, client: FlaskClient) -> None:
        """Proves the form is not reachable signed out."""
        assert client.get("/animals/new").status_code in (302, 401)


class TestCreating:
    """FR-4.1: staff can add an animal."""

    def test_the_form_renders(
        self, staff_client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves the create form is served."""
        response = staff_client.get("/animals/new")

        assert response.status_code == 200
        assert "csrf_token" in response.get_data(as_text=True)

    def test_a_valid_submission_lists_the_animal(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves an animal can enter the system through the application."""
        staff_client.post("/animals/new", data=animal_form())

        created = animal_named(session_factory, "Pepper")
        assert created is not None
        assert created.status == AnimalStatus.AVAILABLE.value
        assert image_count(session_factory, created.animal_id) == 1

    def test_the_new_animal_appears_in_the_public_search(
        self, staff_client: FlaskClient, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves listing an animal actually publishes it.

        A record that exists but never shows up would satisfy the route and
        fail the requirement.
        """
        staff_client.post("/animals/new", data=animal_form())

        assert "Pepper" in client.get("/animals/").get_data(as_text=True)

    def test_an_animal_without_a_photo_is_refused(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves FR-4.2 is enforced, and nothing is written when it fails."""
        response = staff_client.post(
            "/animals/new", data=animal_form(image_urls=[])
        )

        assert response.status_code == 400
        assert animal_named(session_factory, "Pepper") is None

    def test_an_invalid_submission_redisplays_what_was_typed(
        self, staff_client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves a rejected form does not make staff retype everything."""
        response = staff_client.post(
            "/animals/new", data=animal_form(age_years="not a number")
        )
        body = response.get_data(as_text=True)

        assert response.status_code == 400
        assert "Pepper" in body

    def test_an_unknown_species_is_refused(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the enum vocabulary is closed at the HTTP boundary too."""
        response = staff_client.post(
            "/animals/new", data=animal_form(species="DRAGON")
        )

        assert response.status_code == 400
        assert animal_named(session_factory, "Pepper") is None


class TestEditing:
    """FR-4.1: staff can correct a record."""

    def test_an_edit_is_saved(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves changes reach the database."""
        animal_id = world["available_animal_id"]

        staff_client.post(
            f"/animals/{animal_id}/edit",
            data=animal_form(name="Clover", city="Tel Aviv"),
        )

        with session_factory() as session:
            updated = session.get(Animal, animal_id)
            assert updated is not None
            assert updated.city == "Tel Aviv"

    def test_editing_an_animal_that_does_not_exist_is_a_404(
        self, staff_client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves an unknown identifier is not silently ignored."""
        response = staff_client.get(f"/animals/{new_identifier()}/edit")

        assert response.status_code == 404

    def test_an_edit_cannot_remove_the_last_photograph(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves FR-4.2 is checked on edit, not only on creation.

        An animal created with a photo and then edited to have none would
        otherwise walk straight past a rule the create path enforces.
        """
        animal_id = world["available_animal_id"]

        response = staff_client.post(
            f"/animals/{animal_id}/edit", data=animal_form(image_urls=[])
        )

        assert response.status_code == 400
        assert image_count(session_factory, animal_id) >= 1


class TestChangingStatus:
    """FR-4.4: staff can change an animal's availability."""

    def test_a_status_change_is_recorded(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the new status reaches the database."""
        animal_id = world["available_animal_id"]

        staff_client.post(f"/animals/{animal_id}/status", data={"status": "ADOPTED"})

        with session_factory() as session:
            animal = session.get(Animal, animal_id)
            assert animal is not None
            assert animal.status == AnimalStatus.ADOPTED.value

    def test_an_adopted_animal_leaves_the_public_search(
        self, staff_client: FlaskClient, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves the status actually governs what adopters are offered."""
        animal_id = world["available_animal_id"]
        assert "Clover" in client.get("/animals/").get_data(as_text=True)

        staff_client.post(f"/animals/{animal_id}/status", data={"status": "ADOPTED"})

        assert "Clover" not in client.get("/animals/").get_data(as_text=True)

    def test_an_unknown_status_is_refused(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the value is validated rather than written through."""
        animal_id = world["available_animal_id"]

        response = staff_client.post(
            f"/animals/{animal_id}/status", data={"status": "SOLD"}
        )

        assert response.status_code == 400
        with session_factory() as session:
            animal = session.get(Animal, animal_id)
            assert animal is not None
            assert animal.status == AnimalStatus.AVAILABLE.value

    def test_changing_the_status_of_an_unknown_animal_is_a_404(
        self, staff_client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves a bad identifier is reported rather than ignored."""
        response = staff_client.post(
            f"/animals/{new_identifier()}/status", data={"status": "ADOPTED"}
        )

        assert response.status_code == 404
