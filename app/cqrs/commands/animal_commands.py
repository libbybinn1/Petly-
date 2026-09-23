"""Write-side commands for the animal lifecycle (FR-4.1, FR-4.2, FR-4.4).

Before these existed the seed script was the only way an animal entered the
system: staff could search, view and list animals but not add, change or
retire one. That made three MUST requirements unreachable through the
application.

Each command returns an identifier or nothing, never a read DTO - a screen
that needs data after a write dispatches a query next (rule R2).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cqrs.base import Command, CommandHandler
from app.cqrs.commands.application_commands import RecordNotFoundError
from app.domain.animal_rules import AnimalFactsDraft, ensure_animal_has_an_image
from app.domain.enums import AggregateType, AnimalStatus, DomainEventType
from app.eventstore.store import EventStore
from app.infrastructure.models import Animal, AnimalImage, new_identifier


def _now() -> datetime:
    """Naive UTC, matching how DATETIME2 columns are stored."""
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class CreateAnimalCommand(Command):
    """A staff member lists a new animal."""

    animal: AnimalFactsDraft
    staff_user_id: str


class CreateAnimalHandler(CommandHandler[str]):
    """Creates one animal and its photographs."""

    def handle(self, command: Command, session: Session) -> str:
        """Insert the animal, its images, and an event recording the listing.

        Returns:
            The new animal's identifier.

        Raises:
            NoImageError: The animal has no photograph (FR-4.2).
        """
        assert isinstance(command, CreateAnimalCommand)

        draft = command.animal
        ensure_animal_has_an_image(draft.image_urls)

        animal_id = new_identifier()
        created_at = _now()

        session.add(
            Animal(
                animal_id=animal_id,
                name=draft.name,
                species=draft.species.value,
                breed=draft.breed,
                age_years=draft.age_years,
                size=draft.size.value,
                temperament=draft.temperament.value,
                activity_level=draft.activity_level.value,
                good_with_children=draft.good_with_children,
                good_with_other_animals=draft.good_with_other_animals,
                has_special_needs=draft.has_special_needs,
                special_needs_description=draft.special_needs_description,
                required_space=draft.required_space.value,
                city=draft.city,
                status=draft.status.value,
                description=draft.description,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        session.flush()

        _replace_images(session, animal_id, draft.image_urls, created_at)

        EventStore(session).append(
            DomainEventType.ANIMAL_LISTED,
            AggregateType.ANIMAL,
            animal_id,
            payload={"animal_id": animal_id, "name": draft.name},
            actor_user_id=command.staff_user_id,
        )

        return animal_id


@dataclass(frozen=True)
class UpdateAnimalCommand(Command):
    """A staff member corrects or enriches an existing animal's record."""

    animal_id: str
    animal: AnimalFactsDraft
    staff_user_id: str


class UpdateAnimalHandler(CommandHandler[None]):
    """Updates one animal in place."""

    def handle(self, command: Command, session: Session) -> None:
        """Apply the edit, replacing the photo set.

        The image rule is checked here too, not only on creation. An edit
        that removed the last photograph would otherwise walk straight past
        a rule the create path enforces.

        Raises:
            RecordNotFoundError: The animal does not exist.
            NoImageError: The edit would leave it with no photograph.
        """
        assert isinstance(command, UpdateAnimalCommand)

        animal = session.get(Animal, command.animal_id)
        if animal is None:
            raise RecordNotFoundError("Animal does not exist.")

        draft = command.animal
        ensure_animal_has_an_image(draft.image_urls)

        updated_at = _now()
        previous_status = animal.status

        animal.name = draft.name
        animal.species = draft.species.value
        animal.breed = draft.breed
        animal.age_years = draft.age_years
        animal.size = draft.size.value
        animal.temperament = draft.temperament.value
        animal.activity_level = draft.activity_level.value
        animal.good_with_children = draft.good_with_children
        animal.good_with_other_animals = draft.good_with_other_animals
        animal.has_special_needs = draft.has_special_needs
        animal.special_needs_description = draft.special_needs_description
        animal.required_space = draft.required_space.value
        animal.city = draft.city
        animal.description = draft.description
        animal.status = draft.status.value
        animal.updated_at = updated_at

        _replace_images(session, command.animal_id, draft.image_urls, updated_at)

        event_store = EventStore(session)
        event_store.append(
            DomainEventType.ANIMAL_UPDATED,
            AggregateType.ANIMAL,
            command.animal_id,
            payload={"animal_id": command.animal_id, "name": draft.name},
            actor_user_id=command.staff_user_id,
        )

        # A status change carries its own event even when it arrives as part
        # of an edit, so the history reads the same however it happened.
        if previous_status != draft.status.value:
            event_store.append(
                DomainEventType.ANIMAL_STATUS_CHANGED,
                AggregateType.ANIMAL,
                command.animal_id,
                payload={
                    "animal_id": command.animal_id,
                    "from": previous_status,
                    "to": draft.status.value,
                },
                actor_user_id=command.staff_user_id,
            )


@dataclass(frozen=True)
class ChangeAnimalStatusCommand(Command):
    """A staff member changes one animal's availability (FR-4.4)."""

    animal_id: str
    new_status: AnimalStatus
    staff_user_id: str


class ChangeAnimalStatusHandler(CommandHandler[None]):
    """Moves one animal to a new status."""

    def handle(self, command: Command, session: Session) -> None:
        """Record the new status, or do nothing if it is unchanged.

        An unchanged status writes no event. The log is what the history
        view renders, and an entry saying an animal moved from AVAILABLE to
        AVAILABLE is noise that makes the real entries harder to find.

        Raises:
            RecordNotFoundError: The animal does not exist.
        """
        assert isinstance(command, ChangeAnimalStatusCommand)

        animal = session.get(Animal, command.animal_id)
        if animal is None:
            raise RecordNotFoundError("Animal does not exist.")

        if animal.status == command.new_status.value:
            return

        previous_status = animal.status
        animal.status = command.new_status.value
        animal.updated_at = _now()

        EventStore(session).append(
            DomainEventType.ANIMAL_STATUS_CHANGED,
            AggregateType.ANIMAL,
            command.animal_id,
            payload={
                "animal_id": command.animal_id,
                "from": previous_status,
                "to": command.new_status.value,
            },
            actor_user_id=command.staff_user_id,
        )


def _replace_images(
    session: Session, animal_id: str, image_urls: tuple[str, ...], moment: datetime
) -> None:
    """Make the animal's stored photos exactly the given list, in order.

    Replaced wholesale rather than diffed. The form submits the complete
    set it wants, and the first entry is the primary photo - which the
    `uq_animal_primary_image` filtered index requires to be unique per
    animal, so the old rows must be gone before the new ones are inserted.

    Args:
        session: The open write transaction.
        animal_id: The animal whose photos these are.
        image_urls: The complete desired set, primary first.
        moment: The timestamp to record against each row.
    """
    existing = (
        session.execute(
            select(AnimalImage).where(AnimalImage.animal_id == animal_id)
        )
        .scalars()
        .all()
    )
    for row in existing:
        session.delete(row)
    session.flush()

    for position, url in enumerate(image_urls):
        session.add(
            AnimalImage(
                animal_image_id=new_identifier(),
                animal_id=animal_id,
                image_url=url,
                is_primary=position == 0,
                display_order=position,
                uploaded_at=moment,
            )
        )
    session.flush()
