"""Load demo data into the PetMatch database.

Creates staff and adopter accounts, a varied roster of animals with real
downloaded photographs, and a realistic spread of applications and
invitations. Application and invitation history is written through the event
store, so the seeded data exercises the same event-sourced path the running
application uses rather than bypassing it.

Usage:
    .venv/Scripts/python.exe scripts/db.py seed
"""

from __future__ import annotations

import random
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import truststore  # noqa: E402

truststore.inject_into_ssl()

from app.config import load_configuration  # noqa: E402
from app.domain.enums import (  # noqa: E402
    ActivityLevel,
    AggregateType,
    AnimalSize,
    AnimalStatus,
    ApplicationStatus,
    DomainEventType,
    ExperienceLevel,
    HomeType,
    InvitationStatus,
    NotificationType,
    Species,
    Temperament,
    UserRole,
)
from app.eventstore.store import EventStore  # noqa: E402
from app.infrastructure.database import (  # noqa: E402
    create_database_engine,
    create_session_factory,
    session_scope,
)
from app.infrastructure.models import (  # noqa: E402
    AdopterProfile,
    AdoptionApplication,
    AdoptionInvitation,
    Animal,
    AnimalImage,
    Notification,
    User,
    new_identifier,
)
from werkzeug.security import generate_password_hash  # noqa: E402

from scripts.animal_photos import PhotoFetcher  # noqa: E402

# Deterministic seed so repeated runs give the same demo, which makes
# screenshots and test expectations stable.
RANDOM_SEED = 20260922

DEMO_PASSWORD = "Password123!"

CITIES = ("Tel Aviv", "Haifa", "Jerusalem", "Beer Sheva", "Netanya", "Rishon LeZion")


@dataclass
class SeedContext:
    """Collaborators every seeding helper needs.

    Bundled into one object because passing the session, event store,
    randomizer and clock separately to each helper made their signatures
    long enough to obscure the arguments that actually vary.
    """

    session: object  # SQLAlchemy Session
    event_store: EventStore
    randomizer: random.Random
    now: datetime


@dataclass(frozen=True)
class AnimalSpecification:
    """A demo animal defined before persistence."""

    name: str
    species: Species
    breed: str | None
    age_years: float
    size: AnimalSize
    temperament: Temperament
    activity_level: ActivityLevel
    good_with_children: bool
    good_with_other_animals: bool
    has_special_needs: bool
    special_needs_description: str | None
    required_space: AnimalSize
    description: str


ANIMAL_SPECIFICATIONS: tuple[AnimalSpecification, ...] = (
    AnimalSpecification("Luna", Species.DOG, "Border Collie", 2.0, AnimalSize.MEDIUM,
        Temperament.ENERGETIC, ActivityLevel.HIGH, True, True, False, None, AnimalSize.LARGE,
        "Brilliant and tireless. Luna needs a job to do and a person who enjoys long walks."),
    AnimalSpecification("Milo", Species.CAT, "Domestic Shorthair", 4.0, AnimalSize.SMALL,
        Temperament.CALM, ActivityLevel.LOW, True, True, False, None, AnimalSize.SMALL,
        "A quiet lap cat who will find the sunniest spot in any room and stay there."),
    AnimalSpecification("Bella", Species.DOG, "Labrador Retriever", 5.0, AnimalSize.LARGE,
        Temperament.BALANCED, ActivityLevel.MODERATE, True, True, False, None, AnimalSize.LARGE,
        "Gentle, patient and endlessly food-motivated. Wonderful with children."),
    AnimalSpecification("Clover", Species.RABBIT, "Holland Lop", 1.5, AnimalSize.SMALL,
        Temperament.CALM, ActivityLevel.LOW, True, False, False, None, AnimalSize.SMALL,
        "Litter-trained and curious. Happiest with a secure pen and plenty of hay."),
    AnimalSpecification("Pepper", Species.HAMSTER, "Syrian", 0.5, AnimalSize.SMALL,
        Temperament.BALANCED, ActivityLevel.MODERATE, False, False, False, None, AnimalSize.SMALL,
        "A tidy nocturnal companion. Best suited to an older child or adult."),
    AnimalSpecification("Ziggy", Species.BIRD, "Cockatiel", 3.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.MODERATE, True, False, False, None, AnimalSize.SMALL,
        "Whistles a tune he invented himself. Enjoys company and out-of-cage time."),
    AnimalSpecification("Shadow", Species.CAT, "Bombay", 7.0, AnimalSize.MEDIUM,
        Temperament.ANXIOUS, ActivityLevel.LOW, False, False, True,
        "Needs a quiet home; startles easily and hides when overwhelmed.", AnimalSize.SMALL,
        "Shadow takes time to trust, and rewards patience with total devotion."),
    AnimalSpecification("Rocky", Species.DOG, "Jack Russell Terrier", 3.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.HIGH, True, False, False, None, AnimalSize.MEDIUM,
        "Small body, enormous personality. Will out-run anyone who challenges him."),
    AnimalSpecification("Olive", Species.GUINEA_PIG, "Abyssinian", 2.0, AnimalSize.SMALL,
        Temperament.CALM, ActivityLevel.LOW, True, True, False, None, AnimalSize.SMALL,
        "Chatty in the best way. Guinea pigs do best in pairs, and Olive agrees."),
    AnimalSpecification("Atlas", Species.DOG, "German Shepherd", 6.0, AnimalSize.LARGE,
        Temperament.BALANCED, ActivityLevel.HIGH, True, False, False, None, AnimalSize.LARGE,
        "Loyal and highly trainable. Wants a confident owner and a real routine."),
    AnimalSpecification("Poppy", Species.CAT, "Ragdoll", 1.0, AnimalSize.MEDIUM,
        Temperament.CALM, ActivityLevel.LOW, True, True, False, None, AnimalSize.SMALL,
        "Goes limp when picked up, as the breed promises. Adores being carried."),
    AnimalSpecification("Gus", Species.DOG, "Beagle", 8.0, AnimalSize.MEDIUM,
        Temperament.CALM, ActivityLevel.LOW, True, True, True,
        "Senior dog with mild arthritis; needs joint supplements and short walks.",
        AnimalSize.MEDIUM,
        "A gentle old soul who has done his running. Now he would like a sofa."),
    AnimalSpecification("Nova", Species.CAT, "Siamese", 2.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.HIGH, True, True, False, None, AnimalSize.SMALL,
        "Talks constantly and expects an answer. Needs stimulation or she invents it."),
    AnimalSpecification("Biscuit", Species.RABBIT, "Rex", 3.0, AnimalSize.SMALL,
        Temperament.BALANCED, ActivityLevel.MODERATE, True, True, False, None, AnimalSize.SMALL,
        "Velvet-coated and sociable. Enjoys supervised time exploring the room."),
    AnimalSpecification("Kira", Species.DOG, "Siberian Husky", 4.0, AnimalSize.LARGE,
        Temperament.ENERGETIC, ActivityLevel.HIGH, True, True, False, None, AnimalSize.LARGE,
        "Needs serious exercise and a secure garden. Will discuss this loudly."),
    AnimalSpecification("Pumpkin", Species.CAT, "Maine Coon", 5.0, AnimalSize.LARGE,
        Temperament.BALANCED, ActivityLevel.MODERATE, True, True, False, None, AnimalSize.MEDIUM,
        "Enormous, dignified, and convinced he is a small person."),
    AnimalSpecification("Daisy", Species.DOG, "Cavalier King Charles Spaniel", 1.0,
        AnimalSize.SMALL, Temperament.CALM, ActivityLevel.LOW, True, True, False, None,
        AnimalSize.SMALL,
        "An apartment-friendly puppy who mostly wants to be near you."),
    AnimalSpecification("Ash", Species.CAT, "Russian Blue", 9.0, AnimalSize.MEDIUM,
        Temperament.CALM, ActivityLevel.LOW, False, False, True,
        "Senior cat with early kidney disease; requires a prescription diet.",
        AnimalSize.SMALL,
        "Reserved and elegant. Prefers a calm adult household with a routine."),
    AnimalSpecification("Mango", Species.BIRD, "Budgerigar", 1.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.MODERATE, True, False, False, None, AnimalSize.SMALL,
        "Bright, busy and best kept with a companion bird."),
    AnimalSpecification("Bruno", Species.DOG, "Boxer", 7.0, AnimalSize.LARGE,
        Temperament.BALANCED, ActivityLevel.MODERATE, True, False, False, None, AnimalSize.LARGE,
        "Solid, affectionate and slightly clumsy. Great with older children."),
    AnimalSpecification("Willow", Species.RABBIT, "Netherland Dwarf", 0.8, AnimalSize.SMALL,
        Temperament.ANXIOUS, ActivityLevel.LOW, False, False, False, None, AnimalSize.SMALL,
        "Tiny and shy. Needs a gentle, quiet home and time to settle in."),
    AnimalSpecification("Tofu", Species.GUINEA_PIG, "American", 1.0, AnimalSize.SMALL,
        Temperament.CALM, ActivityLevel.LOW, True, True, False, None, AnimalSize.SMALL,
        "Squeaks at the sound of the fridge opening. A cheerful, easy first pet."),
    AnimalSpecification("Juno", Species.DOG, "Greyhound", 5.0, AnimalSize.LARGE,
        Temperament.CALM, ActivityLevel.LOW, True, True, False, None, AnimalSize.MEDIUM,
        "Retired racer. Sprints for ninety seconds, then sleeps for twenty hours."),
    AnimalSpecification("Sesame", Species.CAT, "Tabby", 0.6, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.HIGH, True, True, False, None, AnimalSize.SMALL,
        "A kitten operating at full power at all times. Bring toys."),
    AnimalSpecification("Hazel", Species.DOG, "Poodle", 3.0, AnimalSize.MEDIUM,
        Temperament.BALANCED, ActivityLevel.MODERATE, True, True, False, None, AnimalSize.MEDIUM,
        "Clever and low-shedding. Enjoys training games and puzzle feeders."),
    AnimalSpecification("Pip", Species.HAMSTER, "Dwarf Campbell", 0.4, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.HIGH, False, False, False, None, AnimalSize.SMALL,
        "Fast, tiny and fond of the wheel at three in the morning."),
    AnimalSpecification("Saffron", Species.CAT, "Persian", 6.0, AnimalSize.MEDIUM,
        Temperament.CALM, ActivityLevel.LOW, True, False, True,
        "Long coat requires daily grooming; prone to tear staining.", AnimalSize.SMALL,
        "Serene and high-maintenance in the nicest possible way."),
    AnimalSpecification("Ranger", Species.DOG, "Australian Shepherd", 2.0, AnimalSize.MEDIUM,
        Temperament.ENERGETIC, ActivityLevel.HIGH, True, True, False, None, AnimalSize.LARGE,
        "Needs a job, a garden and a person who likes being outdoors."),
    AnimalSpecification("Peanut", Species.RABBIT, "Lionhead", 2.5, AnimalSize.SMALL,
        Temperament.BALANCED, ActivityLevel.MODERATE, True, True, False, None, AnimalSize.SMALL,
        "A magnificent mane and an agreeable nature. Enjoys gentle handling."),
    AnimalSpecification("Echo", Species.BIRD, "African Grey", 11.0, AnimalSize.MEDIUM,
        Temperament.BALANCED, ActivityLevel.MODERATE, False, False, True,
        "Highly intelligent; needs daily interaction and mental enrichment or "
        "develops feather-plucking.", AnimalSize.MEDIUM,
        "Echo has a vocabulary and opinions. A serious, long-term commitment."),
    AnimalSpecification("Maple", Species.CAT, "Calico", 3.0, AnimalSize.SMALL,
        Temperament.BALANCED, ActivityLevel.MODERATE, True, True, False, None, AnimalSize.SMALL,
        "Independent but affectionate on her own schedule. An easy housemate."),
    AnimalSpecification("Tank", Species.DOG, "Bulldog", 4.0, AnimalSize.MEDIUM,
        Temperament.CALM, ActivityLevel.LOW, True, True, True,
        "Brachycephalic; must avoid heat and strenuous exercise.", AnimalSize.SMALL,
        "Snores impressively. Perfectly content with a short daily amble."),
    AnimalSpecification("Wren", Species.GUINEA_PIG, "Peruvian", 1.5, AnimalSize.SMALL,
        Temperament.ANXIOUS, ActivityLevel.LOW, True, True, False, None, AnimalSize.SMALL,
        "A little timid at first. Bonds strongly once she knows your voice."),
    AnimalSpecification("Koda", Species.DOG, "Alaskan Malamute", 3.0, AnimalSize.LARGE,
        Temperament.ENERGETIC, ActivityLevel.HIGH, True, False, False, None, AnimalSize.LARGE,
        "Powerful and independent. Experienced owners only, please."),
    AnimalSpecification("Clementine", Species.CAT, "Scottish Fold", 2.0, AnimalSize.SMALL,
        Temperament.CALM, ActivityLevel.LOW, True, True, False, None, AnimalSize.SMALL,
        "Sits like a small owl. Gentle, undemanding and very easy company."),
    AnimalSpecification("Bramble", Species.RABBIT, "Flemish Giant", 4.0, AnimalSize.LARGE,
        Temperament.CALM, ActivityLevel.LOW, True, True, False, None, AnimalSize.MEDIUM,
        "Enormous and astonishingly placid. Needs far more space than you expect."),
    AnimalSpecification("Scout", Species.DOG, "Mixed Breed", 1.5, AnimalSize.MEDIUM,
        Temperament.BALANCED, ActivityLevel.MODERATE, True, True, False, None, AnimalSize.MEDIUM,
        "An adaptable, good-natured dog who fits into most households easily."),
    AnimalSpecification("Ivy", Species.CAT, "Domestic Longhair", 10.0, AnimalSize.MEDIUM,
        Temperament.CALM, ActivityLevel.LOW, True, True, True,
        "Senior cat; arthritic and needs a low-sided litter tray.", AnimalSize.SMALL,
        "Twelve years of experience being adored, and keen to continue."),
    AnimalSpecification("Fig", Species.HAMSTER, "Roborovski", 0.3, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.HIGH, False, False, False, None, AnimalSize.SMALL,
        "The smallest and fastest of the hamsters. Better watched than handled."),
    AnimalSpecification("Ranger II", Species.DOG, "Collie Mix", 6.0, AnimalSize.MEDIUM,
        Temperament.BALANCED, ActivityLevel.MODERATE, True, True, False, None, AnimalSize.MEDIUM,
        "Steady, sensible and already house-trained. An easy transition."),
)


@dataclass(frozen=True)
class AdopterSpecification:
    """A demo adopter defined before persistence."""

    full_name: str
    email: str
    home_type: HomeType
    has_yard: bool
    household_has_children: bool
    youngest_child_age: int | None
    has_other_animals: bool
    experience_level: ExperienceLevel
    activity_level: ActivityLevel
    daily_hours_available: float
    preferred_species: str
    open_to_proactive_suggestions: bool


ADOPTER_SPECIFICATIONS: tuple[AdopterSpecification, ...] = (
    AdopterSpecification("Maya Cohen", "maya@example.com", HomeType.APARTMENT, False, False, None,
        False, ExperienceLevel.NONE, ActivityLevel.LOW, 2.0, "CAT,RABBIT", True),
    AdopterSpecification("Daniel Levi", "daniel@example.com", HomeType.HOUSE, True, True, 7,
        True, ExperienceLevel.EXPERIENCED, ActivityLevel.HIGH, 5.0, "DOG", True),
    AdopterSpecification("Noa Friedman", "noa@example.com", HomeType.APARTMENT, False, False, None,
        True, ExperienceLevel.SOME, ActivityLevel.MODERATE, 3.5, "CAT", True),
    AdopterSpecification("Yossi Mizrahi", "yossi@example.com", HomeType.FARM, True, False, None,
        True, ExperienceLevel.EXPERIENCED, ActivityLevel.HIGH, 8.0, "DOG", True),
    AdopterSpecification("Tamar Shapiro", "tamar@example.com", HomeType.APARTMENT, False, True, 12,
        False, ExperienceLevel.SOME, ActivityLevel.MODERATE, 4.0, "CAT,GUINEA_PIG", True),
    AdopterSpecification("Amit Golan", "amit@example.com", HomeType.HOUSE, True, False, None,
        False, ExperienceLevel.NONE, ActivityLevel.MODERATE, 3.0, "DOG,CAT", False),
    AdopterSpecification("Shira Ben-David", "shira@example.com", HomeType.APARTMENT, False, False,
        None, False, ExperienceLevel.SOME, ActivityLevel.LOW, 2.5, "RABBIT,HAMSTER", True),
    AdopterSpecification("Eitan Barak", "eitan@example.com", HomeType.HOUSE, True, True, 4,
        False, ExperienceLevel.EXPERIENCED, ActivityLevel.HIGH, 6.0, "DOG", True),
    AdopterSpecification("Liora Katz", "liora@example.com", HomeType.APARTMENT, False, False, None,
        True, ExperienceLevel.EXPERIENCED, ActivityLevel.LOW, 5.0, "CAT", True),
    AdopterSpecification("Omer Peretz", "omer@example.com", HomeType.HOUSE, True, False, None,
        False, ExperienceLevel.SOME, ActivityLevel.HIGH, 4.5, "DOG", False),
    AdopterSpecification("Rivka Adler", "rivka@example.com", HomeType.APARTMENT, False, True, 9,
        False, ExperienceLevel.NONE, ActivityLevel.LOW, 2.0, "GUINEA_PIG,RABBIT", True),
    AdopterSpecification("Gal Rosen", "gal@example.com", HomeType.HOUSE, True, False, None,
        True, ExperienceLevel.EXPERIENCED, ActivityLevel.MODERATE, 5.5, "DOG,CAT", True),
)

STAFF_SPECIFICATIONS = (
    ("Dana Aviv", "dana@petmatch.org"),
    ("Itai Segal", "itai@petmatch.org"),
)


def _build_users(now: datetime) -> tuple[list[User], list[User]]:
    """Create the staff and adopter user records."""
    password_hash = generate_password_hash(DEMO_PASSWORD)

    staff_users = [
        User(
            user_id=new_identifier(),
            email=email,
            password_hash=password_hash,
            full_name=name,
            role=UserRole.STAFF.value,
            is_active=True,
            created_at=now,
        )
        for name, email in STAFF_SPECIFICATIONS
    ]

    adopter_users = [
        User(
            user_id=new_identifier(),
            email=specification.email,
            password_hash=password_hash,
            full_name=specification.full_name,
            role=UserRole.ADOPTER.value,
            is_active=True,
            created_at=now,
        )
        for specification in ADOPTER_SPECIFICATIONS
    ]

    return staff_users, adopter_users


def _build_adopter_profiles(
    adopter_users: list[User], randomizer: random.Random, now: datetime
) -> list[AdopterProfile]:
    """Create a completed profile for each adopter."""
    profiles = []
    for user, specification in zip(adopter_users, ADOPTER_SPECIFICATIONS, strict=True):
        profiles.append(
            AdopterProfile(
                adopter_profile_id=new_identifier(),
                user_id=user.user_id,
                home_type=specification.home_type.value,
                has_yard=specification.has_yard,
                yard_size_sqm=randomizer.choice([40, 80, 150]) if specification.has_yard else None,
                household_has_children=specification.household_has_children,
                youngest_child_age=specification.youngest_child_age,
                has_other_animals=specification.has_other_animals,
                other_animals_description=(
                    "One resident cat" if specification.has_other_animals else None
                ),
                experience_level=specification.experience_level.value,
                activity_level=specification.activity_level.value,
                daily_hours_available=specification.daily_hours_available,
                city=randomizer.choice(CITIES),
                preferred_species=specification.preferred_species,
                preferred_size=None,
                preferred_age_range=None,
                open_to_proactive_suggestions=specification.open_to_proactive_suggestions,
                is_complete=True,
                created_at=now,
                updated_at=now,
            )
        )
    return profiles


def _build_animals(
    randomizer: random.Random,
    now: datetime,
    upload_directory: Path,
    photo_fetcher: PhotoFetcher,
) -> tuple[list[Animal], list[AnimalImage], int]:
    """Create animals and download a photograph for each one."""
    animals: list[Animal] = []
    images: list[AnimalImage] = []
    downloaded_count = 0

    # A realistic roster is mostly available, with a few further along the
    # pipeline so the dashboard has something meaningful to show.
    statuses = (
        [AnimalStatus.AVAILABLE] * 32
        + [AnimalStatus.RESERVED] * 3
        + [AnimalStatus.ADOPTION_IN_PROGRESS] * 2
        + [AnimalStatus.ADOPTED] * 2
        + [AnimalStatus.UNAVAILABLE]
    )

    for index, specification in enumerate(ANIMAL_SPECIFICATIONS):
        animal_id = new_identifier()
        status = statuses[index] if index < len(statuses) else AnimalStatus.AVAILABLE

        animals.append(
            Animal(
                animal_id=animal_id,
                name=specification.name,
                species=specification.species.value,
                breed=specification.breed,
                age_years=specification.age_years,
                size=specification.size.value,
                temperament=specification.temperament.value,
                activity_level=specification.activity_level.value,
                good_with_children=specification.good_with_children,
                good_with_other_animals=specification.good_with_other_animals,
                has_special_needs=specification.has_special_needs,
                special_needs_description=specification.special_needs_description,
                required_space=specification.required_space.value,
                city=randomizer.choice(CITIES),
                status=status.value,
                description=specification.description,
                created_at=now,
                updated_at=now,
            )
        )

        file_name = f"{animal_id}.jpg"
        was_downloaded = photo_fetcher.fetch_into(
            specification.species, specification.breed, upload_directory / file_name
        )
        if was_downloaded:
            downloaded_count += 1
            images.append(
                AnimalImage(
                    animal_image_id=new_identifier(),
                    animal_id=animal_id,
                    image_url=f"/static/uploads/{file_name}",
                    is_primary=True,
                    display_order=0,
                    uploaded_at=now,
                )
            )
        print(f"  [{index + 1:>2}/{len(ANIMAL_SPECIFICATIONS)}] {specification.name:<12}"
              f"{'photo ok' if was_downloaded else 'photo unavailable'}")

    return animals, images, downloaded_count


def run_seed() -> int:
    """Populate the database with demo data.

    Returns:
        Process exit code: 0 on success.
    """
    configuration = load_configuration()
    randomizer = random.Random(RANDOM_SEED)
    now = datetime.now(UTC).replace(tzinfo=None)

    upload_directory = configuration.upload_directory
    upload_directory.mkdir(parents=True, exist_ok=True)

    engine = create_database_engine(configuration)
    session_factory = create_session_factory(engine)

    print("downloading animal photographs and building records...")
    staff_users, adopter_users = _build_users(now)
    profiles = _build_adopter_profiles(adopter_users, randomizer, now)
    photo_fetcher = PhotoFetcher(randomizer=randomizer)
    animals, images, downloaded_count = _build_animals(
        randomizer, now, upload_directory, photo_fetcher
    )

    with session_scope(session_factory) as session:
        session.add_all(staff_users)
        session.add_all(adopter_users)
        session.flush()
        session.add_all(profiles)
        session.add_all(animals)
        session.flush()
        session.add_all(images)
        session.flush()

        context = SeedContext(
            session=session,
            event_store=EventStore(session),
            randomizer=randomizer,
            now=now,
        )
        applications = _seed_applications(context, profiles, animals)
        invitations = _seed_invitations(context, profiles, animals, staff_users[0])

    print(f"\nseeded {len(staff_users)} staff, {len(adopter_users)} adopters, "
          f"{len(animals)} animals ({downloaded_count} photos), "
          f"{applications} applications, {invitations} invitations")
    print(f"\nlog in with any listed email and the password: {DEMO_PASSWORD}")
    print(f"  staff   : {STAFF_SPECIFICATIONS[0][1]}")
    print(f"  adopter : {ADOPTER_SPECIFICATIONS[0].email}")
    return 0


def _seed_applications(
    context: SeedContext,
    profiles: list[AdopterProfile],
    animals: list[Animal],
) -> int:
    """Create applications, appending the matching events to the log."""
    available_animals = [a for a in animals if a.status == AnimalStatus.AVAILABLE.value]
    created = 0

    for profile in profiles:
        application_count = context.randomizer.choice([0, 1, 1, 2, 2, 3])
        take = min(application_count, len(available_animals))
        chosen = context.randomizer.sample(available_animals, take)

        for animal in chosen:
            submitted_at = context.now - timedelta(days=context.randomizer.randint(1, 21))
            status = context.randomizer.choice(
                [ApplicationStatus.SUBMITTED] * 3 + [ApplicationStatus.UNDER_REVIEW] * 2
            )
            application_id = new_identifier()

            context.session.add(
                AdoptionApplication(
                    application_id=application_id,
                    adopter_profile_id=profile.adopter_profile_id,
                    animal_id=animal.animal_id,
                    status=status.value,
                    applicant_message=(
                        f"I would love to meet {animal.name}. I think we would suit each other."
                    ),
                    submitted_at=submitted_at,
                )
            )
            context.event_store.append(
                DomainEventType.APPLICATION_SUBMITTED,
                AggregateType.APPLICATION,
                application_id,
                payload={"animal_id": animal.animal_id, "animal_name": animal.name},
                actor_user_id=profile.user_id,
                occurred_at=submitted_at.replace(tzinfo=UTC),
            )
            if status is ApplicationStatus.UNDER_REVIEW:
                context.event_store.append(
                    DomainEventType.APPLICATION_UNDER_REVIEW,
                    AggregateType.APPLICATION,
                    application_id,
                    payload={"animal_id": animal.animal_id},
                    occurred_at=(submitted_at + timedelta(days=1)).replace(tzinfo=UTC),
                )
            created += 1

    return created


def _seed_invitations(
    context: SeedContext,
    profiles: list[AdopterProfile],
    animals: list[Animal],
    sending_staff: User,
) -> int:
    """Create invitations in a spread of states, including expired ones."""
    opted_in = [p for p in profiles if p.open_to_proactive_suggestions]
    available_animals = [a for a in animals if a.status == AnimalStatus.AVAILABLE.value]
    created = 0

    for _ in range(8):
        profile = context.randomizer.choice(opted_in)
        animal = context.randomizer.choice(available_animals)
        # Some invitations are deliberately older than the 72-hour window so
        # the dashboard's "expired" figure is not always zero.
        sent_at = context.now - timedelta(hours=context.randomizer.choice([2, 10, 30, 50, 80, 120]))
        expires_at = sent_at + timedelta(hours=72)

        if expires_at < context.now:
            status = InvitationStatus.EXPIRED
        else:
            status = context.randomizer.choice(
                [InvitationStatus.SENT, InvitationStatus.VIEWED, InvitationStatus.ACCEPTED]
            )

        invitation_id = new_identifier()
        context.session.add(
            AdoptionInvitation(
                invitation_id=invitation_id,
                animal_id=animal.animal_id,
                adopter_profile_id=profile.adopter_profile_id,
                sent_by_user_id=sending_staff.user_id,
                status=status.value,
                staff_message=(
                    f"We thought {animal.name} might be a good fit for your home. "
                    "Would you like to meet?"
                ),
                sent_at=sent_at,
                expires_at=expires_at,
                viewed_at=(
                    sent_at + timedelta(hours=1)
                    if status is not InvitationStatus.SENT
                    else None
                ),
                responded_at=(
                    sent_at + timedelta(hours=2)
                    if status is InvitationStatus.ACCEPTED
                    else None
                ),
            )
        )
        context.event_store.append(
            DomainEventType.INVITATION_SENT,
            AggregateType.INVITATION,
            invitation_id,
            payload={"animal_id": animal.animal_id, "animal_name": animal.name},
            actor_user_id=sending_staff.user_id,
            occurred_at=sent_at.replace(tzinfo=UTC),
        )
        if status is InvitationStatus.EXPIRED:
            context.event_store.append(
                DomainEventType.INVITATION_EXPIRED,
                AggregateType.INVITATION,
                invitation_id,
                payload={"animal_id": animal.animal_id},
                occurred_at=expires_at.replace(tzinfo=UTC),
            )
        elif status is InvitationStatus.ACCEPTED:
            context.event_store.append(
                DomainEventType.INVITATION_ACCEPTED,
                AggregateType.INVITATION,
                invitation_id,
                payload={"animal_id": animal.animal_id},
                actor_user_id=profile.user_id,
                occurred_at=(sent_at + timedelta(hours=2)).replace(tzinfo=UTC),
            )

        context.session.add(
            Notification(
                notification_id=new_identifier(),
                user_id=profile.user_id,
                notification_type=NotificationType.INVITATION_RECEIVED.value,
                title=f"Invitation to meet {animal.name}",
                body=f"{sending_staff.full_name} thinks {animal.name} could suit your home.",
                link_url="/my/invitations",
                is_read=status is not InvitationStatus.SENT,
                created_at=sent_at,
            )
        )
        created += 1

    return created


if __name__ == "__main__":
    sys.exit(run_seed())
