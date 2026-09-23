"""Load demo data into the PetMatch database.

This module owns the records that simply exist - staff and adopter accounts,
adopter profiles, and the animal roster with a real downloaded photograph for
each animal. The records that have a past, meaning applications and
invitations and their event streams, are written by
`scripts/seed_history.py`.

The data itself lives apart from the loading logic, so the roster can be
checked without a database or a network:

    scripts/seed_roster.py    the animals
    scripts/seed_people.py    the adopters and staff
    scripts/seed_history.py   applications, invitations, stored analyses
    tests/unit/test_seed_data.py  proves the data is internally consistent

Usage:
    <python> scripts/db.py seed
"""

from __future__ import annotations

import random
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import truststore  # noqa: E402

truststore.inject_into_ssl()

from app.config import load_configuration  # noqa: E402
from app.domain.enums import UserRole  # noqa: E402
from app.eventstore.store import EventStore  # noqa: E402
from app.infrastructure.database import (  # noqa: E402
    create_database_engine,
    create_session_factory,
    session_scope,
)
from app.infrastructure.models import (  # noqa: E402
    AdopterProfile,
    Animal,
    AnimalImage,
    User,
    new_identifier,
)
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

from scripts.animal_photos import PhotoFetcher  # noqa: E402
from scripts.seed_history import (  # noqa: E402
    HistoryCounts,
    SeedContext,
    write_history,
)
from scripts.seed_people import (  # noqa: E402
    ADOPTER_SPECIFICATIONS,
    DEMO_ADOPTER_EMAIL,
    DEMO_STAFF_EMAIL,
    STAFF_SPECIFICATIONS,
    AdopterSpecification,
)
from scripts.seed_roster import (  # noqa: E402
    ANIMAL_SPECIFICATIONS,
    CITIES,
    AnimalSpecification,
    kind_group,
    status_for,
)

# Deterministic seed so repeated runs give the same demo, which makes
# screenshots and test expectations stable.
RANDOM_SEED = 20260922

DEMO_PASSWORD = "Password123!"

# A single shared photograph, used only when a live fetch failed for one
# animal. Spec 24 makes an image mandatory, so an animal with no picture would
# be a broken listing rather than a tidy blank.
FALLBACK_PHOTOGRAPH_NAME = "sample.jpg"
FALLBACK_PHOTOGRAPH_URL = f"/static/uploads/{FALLBACK_PHOTOGRAPH_NAME}"


@dataclass
class AnimalRoster:
    """The animal records and the outcome of fetching their photographs."""

    animals: list[Animal] = field(default_factory=list)
    images: list[AnimalImage] = field(default_factory=list)
    downloaded_count: int = 0
    fallback_count: int = 0


@dataclass(frozen=True)
class DemoPeople:
    """Every account the demo contains."""

    staff_users: list[User]
    adopter_users: list[User]
    profiles: list[AdopterProfile]


# --------------------------------------------------------------------------
# Accounts and profiles
# --------------------------------------------------------------------------


def _build_users(now: datetime) -> DemoPeople:
    """Create the staff and adopter accounts and the adopter profiles."""
    password_hash = generate_password_hash(DEMO_PASSWORD)

    staff_users = [
        User(
            user_id=new_identifier(),
            email=specification.email,
            password_hash=password_hash,
            full_name=specification.full_name,
            role=UserRole.STAFF.value,
            is_active=True,
            created_at=now,
        )
        for specification in STAFF_SPECIFICATIONS
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

    profiles = [
        _build_adopter_profile(user, specification, now)
        for user, specification in zip(adopter_users, ADOPTER_SPECIFICATIONS, strict=True)
    ]

    return DemoPeople(staff_users=staff_users, adopter_users=adopter_users, profiles=profiles)


def _build_adopter_profile(
    user: User, specification: AdopterSpecification, now: datetime
) -> AdopterProfile:
    """Turn one adopter specification into a profile row (spec section 5.1)."""
    preferred_size = specification.preferred_size
    return AdopterProfile(
        adopter_profile_id=new_identifier(),
        user_id=user.user_id,
        home_type=specification.home_type.value,
        has_yard=specification.has_yard,
        yard_size_sqm=specification.yard_size_sqm,
        household_has_children=specification.household_has_children,
        youngest_child_age=specification.youngest_child_age,
        has_other_animals=specification.has_other_animals,
        other_animals_description=specification.other_animals_description,
        experience_level=specification.experience_level.value,
        activity_level=specification.activity_level.value,
        daily_hours_available=specification.daily_hours_available,
        city=specification.city,
        preferred_species=specification.preferred_species_column,
        preferred_size=preferred_size.value if preferred_size is not None else None,
        preferred_age_range=specification.preferred_age_range.value,
        open_to_proactive_suggestions=specification.open_to_proactive_suggestions,
        is_complete=specification.is_complete,
        created_at=now,
        updated_at=now,
    )


# --------------------------------------------------------------------------
# Animals and their photographs
# --------------------------------------------------------------------------


def _build_animals(
    randomizer: random.Random,
    now: datetime,
    upload_directory: Path,
    photo_fetcher: PhotoFetcher,
) -> AnimalRoster:
    """Create every animal and download a photograph for each one.

    Photographs are fetched first and the image rows built afterwards, because
    an animal whose own fetch failed borrows the shared fallback picture - and
    that file may itself be one of this run's downloads.
    """
    roster = AnimalRoster()
    photograph_names: dict[str, str | None] = {}

    for index, specification in enumerate(ANIMAL_SPECIFICATIONS):
        animal = _build_animal(specification, index, randomizer, now)
        roster.animals.append(animal)
        photograph_names[animal.animal_id] = _fetch_photograph(
            specification, animal.animal_id, upload_directory, photo_fetcher
        )
        _report_progress(index, specification, photograph_names[animal.animal_id])

    has_fallback = _ensure_fallback_photograph(upload_directory, photograph_names)
    _attach_images(roster, photograph_names, has_fallback, now)
    return roster


def _build_animal(
    specification: AnimalSpecification,
    roster_index: int,
    randomizer: random.Random,
    now: datetime,
) -> Animal:
    """Turn one roster entry into an animal row (spec section 5.2)."""
    return Animal(
        animal_id=new_identifier(),
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
        status=status_for(specification, roster_index).value,
        description=specification.description,
        created_at=now,
        updated_at=now,
    )


def _fetch_photograph(
    specification: AnimalSpecification,
    animal_id: str,
    upload_directory: Path,
    photo_fetcher: PhotoFetcher,
) -> str | None:
    """Download one animal's photograph, returning its file name or None.

    The fetcher decides the extension from what the server sent, so the name
    it reports back is the one to store rather than the one asked for.
    """
    written = photo_fetcher.fetch_into(
        specification.species, specification.breed, upload_directory / f"{animal_id}.jpg"
    )
    return written.name if written is not None else None


def _report_progress(
    index: int, specification: AnimalSpecification, file_name: str | None
) -> None:
    """Print one line per animal so a long fetch does not look like a hang."""
    outcome = "photo ok" if file_name else "photo unavailable - using the fallback"
    print(
        f"  [{index + 1:>3}/{len(ANIMAL_SPECIFICATIONS)}] "
        f"{specification.name:<14}{kind_group(specification):<14}{outcome}"
    )


def _ensure_fallback_photograph(
    upload_directory: Path, photograph_names: dict[str, str | None]
) -> bool:
    """Make sure a shared fallback picture exists on disk.

    The end-to-end suite also expects `sample.jpg` to be present, and the
    uploads directory is gitignored, so a fresh clone has none until a seed
    has run. Creating it here makes the file a side effect of seeding rather
    than something a developer has to remember.

    Args:
        upload_directory: Where animal photographs are written.
        photograph_names: File names of this run's successful downloads.

    Returns:
        Whether a usable fallback file is now present.
    """
    fallback_path = upload_directory / FALLBACK_PHOTOGRAPH_NAME
    if fallback_path.exists():
        return True

    # A JPEG specifically, because the fallback is served under a .jpg name.
    first_download = next(
        (name for name in photograph_names.values() if name and name.endswith(".jpg")), None
    )
    if first_download is None:
        return False

    fallback_path.write_bytes((upload_directory / first_download).read_bytes())
    return True


def _attach_images(
    roster: AnimalRoster,
    photograph_names: dict[str, str | None],
    has_fallback: bool,
    now: datetime,
) -> None:
    """Build one primary image row per animal (spec section 24).

    Exactly one, because `uq_animal_primary_image` is unique on the animal
    where `is_primary` is set.
    """
    for animal in roster.animals:
        file_name = photograph_names[animal.animal_id]
        if file_name:
            roster.downloaded_count += 1
            image_url = f"/static/uploads/{file_name}"
        elif has_fallback:
            roster.fallback_count += 1
            image_url = FALLBACK_PHOTOGRAPH_URL
        else:
            continue

        roster.images.append(
            AnimalImage(
                animal_image_id=new_identifier(),
                animal_id=animal.animal_id,
                image_url=image_url,
                is_primary=True,
                display_order=0,
                uploaded_at=now,
            )
        )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _write_demo_data(
    session_factory: sessionmaker[Session],
    people: DemoPeople,
    roster: AnimalRoster,
    randomizer: random.Random,
    now: datetime,
) -> HistoryCounts:
    """Write every record in one transaction and report what was written."""
    with session_scope(session_factory) as session:
        session.add_all(people.staff_users)
        session.add_all(people.adopter_users)
        session.flush()
        session.add_all(people.profiles)
        session.add_all(roster.animals)
        session.flush()
        session.add_all(roster.images)
        session.flush()

        context = SeedContext(
            session=session,
            event_store=EventStore(session),
            randomizer=randomizer,
            now=now,
            staff_users=people.staff_users,
        )
        return write_history(context, people.profiles, roster.animals)


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

    session_factory = create_session_factory(create_database_engine(configuration))

    print(f"building {len(ANIMAL_SPECIFICATIONS)} animals and downloading photographs...")
    people = _build_users(now)
    # The fetcher gets its own randomizer rather than sharing this run's. How
    # many times it shuffles a candidate list depends on how the photo
    # services answer, and a shared stream would let a flaky download change
    # which city an animal lives in and who applied for it. The seeded data
    # has to be reproducible whether or not the network cooperated.
    roster = _build_animals(
        randomizer,
        now,
        upload_directory,
        PhotoFetcher(randomizer=random.Random(RANDOM_SEED)),
    )
    counts = _write_demo_data(session_factory, people, roster, randomizer, now)

    _print_summary(people, roster, counts)
    return 0


def _print_summary(people: DemoPeople, roster: AnimalRoster, counts: HistoryCounts) -> None:
    """Report what was seeded, grouped by kind of animal."""
    by_kind: dict[str, int] = defaultdict(int)
    for specification in ANIMAL_SPECIFICATIONS:
        by_kind[kind_group(specification)] += 1

    print(
        f"\nseeded {len(people.staff_users)} staff, {len(people.adopter_users)} adopters, "
        f"{len(roster.animals)} animals, {counts.applications} applications "
        f"({counts.closed_by_cascade} closed by the approval cascade), "
        f"{counts.invitations} invitations, {counts.analyses} stored analyses, "
        f"{counts.status_changes} recorded status changes"
    )
    print(
        f"photographs: {roster.downloaded_count} downloaded, "
        f"{roster.fallback_count} using the shared fallback"
    )
    print("\nanimals by kind:")
    for kind, count in sorted(by_kind.items(), key=lambda pair: (-pair[1], pair[0])):
        print(f"  {kind:<16}{count:>4}")

    print(f"\nlog in with any listed email and the password: {DEMO_PASSWORD}")
    print(f"  staff   : {DEMO_STAFF_EMAIL}")
    print(f"  adopter : {DEMO_ADOPTER_EMAIL}")


if __name__ == "__main__":
    sys.exit(run_seed())
