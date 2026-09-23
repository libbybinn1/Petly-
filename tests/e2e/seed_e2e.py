"""Deterministic seed data for the end-to-end suite.

Small and fixed on purpose: every E2E assertion should be able to name the
record it depends on, rather than hoping the shared demo data still happens
to contain something suitable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.domain.enums import (
    ActivityLevel,
    AggregateType,
    AnimalSize,
    AnimalStatus,
    ApplicationStatus,
    DomainEventType,
    ExperienceLevel,
    HomeType,
    InvitationStatus,
    Species,
    Temperament,
    UserRole,
)
from app.eventstore.store import EventStore
from app.infrastructure.database import Base
from app.infrastructure.models import (
    AdopterProfile,
    AdoptionApplication,
    AdoptionInvitation,
    Animal,
    AnimalImage,
    User,
    new_identifier,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from werkzeug.security import generate_password_hash

STAFF_EMAIL = "dana@petmatch.org"
ADOPTER_EMAIL = "maya@example.com"
SECOND_ADOPTER_EMAIL = "daniel@example.com"
DEMO_PASSWORD = "Password123!"

# A real seeded image, so the interface-quality test sees a genuine <img>
# rather than the placeholder fallback.
SAMPLE_IMAGE_URL = "/static/uploads/sample.jpg"


def _now() -> datetime:
    """Naive UTC, matching how the application stores timestamps."""
    return datetime.now(UTC).replace(tzinfo=None)


def seed_e2e_database(database_url: str) -> None:
    """Create and populate a database for the end-to-end suite.

    Args:
        database_url: A SQLAlchemy URL for a fresh database.
    """
    engine = create_engine(database_url, future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    password_hash = generate_password_hash(DEMO_PASSWORD)

    with session_factory() as session:
        staff = User(
            user_id=new_identifier(), email=STAFF_EMAIL, password_hash=password_hash,
            full_name="Dana Aviv", role=UserRole.STAFF.value,
            is_active=True, created_at=_now(),
        )
        session.add(staff)

        adopters: dict[str, AdopterProfile] = {}
        for email, name, opted_in in (
            (ADOPTER_EMAIL, "Maya Cohen", True),
            (SECOND_ADOPTER_EMAIL, "Daniel Levi", True),
        ):
            user = User(
                user_id=new_identifier(), email=email, password_hash=password_hash,
                full_name=name, role=UserRole.ADOPTER.value,
                is_active=True, created_at=_now(),
            )
            profile = AdopterProfile(
                adopter_profile_id=new_identifier(), user_id=user.user_id,
                home_type=HomeType.APARTMENT.value, has_yard=False,
                household_has_children=False, has_other_animals=False,
                experience_level=ExperienceLevel.SOME.value,
                activity_level=ActivityLevel.LOW.value,
                daily_hours_available=3.0, city="Haifa",
                preferred_species=f"{Species.CAT.value},{Species.RABBIT.value}",
                open_to_proactive_suggestions=opted_in, is_complete=True,
                created_at=_now(), updated_at=_now(),
            )
            session.add_all([user, profile])
            adopters[email] = profile

        animals = _create_animals(session)
        session.flush()

        event_store = EventStore(session)
        _create_application(
            session, event_store, adopters[ADOPTER_EMAIL], animals["cat"]
        )
        _create_application(
            session, event_store, adopters[SECOND_ADOPTER_EMAIL], animals["cat"]
        )
        _create_invitation(session, event_store, staff, adopters[ADOPTER_EMAIL],
                           animals["rabbit"])

        session.commit()

    engine.dispose()


def _create_animals(session: Session) -> dict[str, Animal]:
    """Create a small roster covering the cases the tests rely on."""
    specifications = (
        ("cat", "Milo", Species.CAT, AnimalSize.SMALL, ActivityLevel.LOW,
         Temperament.CALM, AnimalStatus.AVAILABLE),
        ("cat_two", "Poppy", Species.CAT, AnimalSize.SMALL, ActivityLevel.LOW,
         Temperament.CALM, AnimalStatus.AVAILABLE),
        ("rabbit", "Clover", Species.RABBIT, AnimalSize.SMALL, ActivityLevel.LOW,
         Temperament.CALM, AnimalStatus.AVAILABLE),
        ("dog", "Bella", Species.DOG, AnimalSize.MEDIUM, ActivityLevel.MODERATE,
         Temperament.BALANCED, AnimalStatus.AVAILABLE),
        ("adopted", "Gus", Species.DOG, AnimalSize.MEDIUM, ActivityLevel.LOW,
         Temperament.CALM, AnimalStatus.ADOPTED),
    )

    created: dict[str, Animal] = {}
    for key, name, species, size, activity, temperament, status in specifications:
        animal = Animal(
            animal_id=new_identifier(), name=name, species=species.value,
            breed=None, age_years=3.0, size=size.value,
            temperament=temperament.value, activity_level=activity.value,
            good_with_children=True, good_with_other_animals=True,
            has_special_needs=False, required_space=size.value, city="Haifa",
            status=status.value, description=f"{name} is looking for a home.",
            created_at=_now(), updated_at=_now(),
        )
        session.add(animal)
        session.flush()
        session.add(
            AnimalImage(
                animal_image_id=new_identifier(), animal_id=animal.animal_id,
                image_url=SAMPLE_IMAGE_URL, is_primary=True,
                display_order=0, uploaded_at=_now(),
            )
        )
        created[key] = animal

    return created


def _create_application(
    session: Session,
    event_store: EventStore,
    profile: AdopterProfile,
    animal: Animal,
) -> None:
    """Add one submitted application with its event."""
    application_id = new_identifier()
    submitted_at = _now() - timedelta(days=2)

    session.add(
        AdoptionApplication(
            application_id=application_id,
            adopter_profile_id=profile.adopter_profile_id,
            animal_id=animal.animal_id,
            status=ApplicationStatus.SUBMITTED.value,
            applicant_message=f"I would love to meet {animal.name}.",
            submitted_at=submitted_at,
        )
    )
    event_store.append(
        DomainEventType.APPLICATION_SUBMITTED,
        AggregateType.APPLICATION,
        application_id,
        payload={"animal_id": animal.animal_id, "animal_name": animal.name},
        actor_user_id=profile.user_id,
        occurred_at=submitted_at.replace(tzinfo=UTC),
    )


def _create_invitation(
    session: Session,
    event_store: EventStore,
    staff: User,
    profile: AdopterProfile,
    animal: Animal,
) -> None:
    """Add one open invitation, well inside its 72-hour window."""
    invitation_id = new_identifier()
    sent_at = _now() - timedelta(hours=4)

    session.add(
        AdoptionInvitation(
            invitation_id=invitation_id,
            animal_id=animal.animal_id,
            adopter_profile_id=profile.adopter_profile_id,
            sent_by_user_id=staff.user_id,
            status=InvitationStatus.SENT.value,
            staff_message=f"We thought {animal.name} might suit your home.",
            sent_at=sent_at,
            expires_at=sent_at + timedelta(hours=72),
        )
    )
    event_store.append(
        DomainEventType.INVITATION_SENT,
        AggregateType.INVITATION,
        invitation_id,
        payload={"animal_id": animal.animal_id, "animal_name": animal.name},
        actor_user_id=staff.user_id,
        occurred_at=sent_at.replace(tzinfo=UTC),
    )
