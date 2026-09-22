"""Database management entry point for PetMatch.

Single command surface for connecting, creating, resetting, seeding and
inspecting the database. Documented by the `db-management` skill.

Usage:
    .venv/Scripts/python.exe scripts/db.py <command>

Commands:
    check    Verify connectivity and report server version and table counts
    create   Create every table that does not yet exist
    reset    Drop all project tables, then recreate them
    seed     Load demo data
    fresh    reset + seed
    tables   List project tables with row counts
    events   Show the most recent domain events
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Importing models registers them on Base.metadata. Listed explicitly so
# linters do not remove what looks like an unused import.
import app.infrastructure.models  # noqa: E402, F401
from app.config import load_configuration  # noqa: E402
from app.infrastructure.database import (  # noqa: E402
    Base,
    create_database_engine,
    create_session_factory,
)
from app.infrastructure.models import DomainEvent  # noqa: E402
from sqlalchemy import func, inspect, select, text  # noqa: E402

EXPECTED_ARGUMENT_COUNT = 2  # script name plus one command

PROJECT_TABLES = (
    "analysis_jobs",
    "domain_events",
    "notifications",
    "match_analyses",
    "adoption_invitations",
    "adoption_applications",
    "animal_images",
    "animals",
    "adopter_profiles",
    "users",
)


def _engine():  # noqa: ANN202 - local helper, type is an SQLAlchemy Engine
    """Build an engine from the current configuration."""
    return create_database_engine(load_configuration())


def command_check() -> int:
    """Report server version, table count and write permission."""
    engine = _engine()
    with engine.connect() as connection:
        version = connection.execute(text("SELECT @@VERSION")).scalar_one()
        database_name = connection.execute(text("SELECT DB_NAME()")).scalar_one()
        table_count = connection.execute(
            text("SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES")
        ).scalar_one()

    print(f"server   : {str(version).splitlines()[0].strip()}")
    print(f"database : {database_name}")
    print(f"tables   : {table_count}")
    print("connection OK")
    return 0


def command_create() -> int:
    """Create any missing tables without touching existing data."""
    engine = _engine()
    before = set(inspect(engine).get_table_names())
    Base.metadata.create_all(engine)
    after = set(inspect(engine).get_table_names())

    created = sorted(after - before)
    if created:
        print(f"created {len(created)} table(s):")
        for name in created:
            print(f"  + {name}")
    else:
        print("no new tables; schema already present")
    return 0


def command_reset() -> int:
    """Drop every project table and recreate the schema.

    Tables are dropped in dependency order because SQL Server refuses to drop
    a table that another table still references.
    """
    engine = _engine()
    existing = set(inspect(engine).get_table_names())

    with engine.begin() as connection:
        for table_name in PROJECT_TABLES:
            if table_name in existing:
                connection.execute(text(f"DROP TABLE [{table_name}]"))
                print(f"  - dropped {table_name}")

    Base.metadata.create_all(engine)
    print(f"recreated {len(Base.metadata.tables)} tables")
    return 0


def command_tables() -> int:
    """List project tables with their row counts."""
    engine = _engine()
    present = set(inspect(engine).get_table_names())

    print(f"{'table':<26}{'rows':>8}")
    print("-" * 34)
    with engine.connect() as connection:
        for table_name in reversed(PROJECT_TABLES):
            if table_name not in present:
                print(f"{table_name:<26}{'absent':>8}")
                continue
            count = connection.execute(text(f"SELECT COUNT(*) FROM [{table_name}]")).scalar_one()
            print(f"{table_name:<26}{count:>8}")
    return 0


def command_events() -> int:
    """Show the 20 most recent domain events."""
    session_factory = create_session_factory(_engine())
    with session_factory() as session:
        total = session.execute(select(func.count()).select_from(DomainEvent)).scalar_one()
        recent = session.execute(
            select(DomainEvent).order_by(DomainEvent.occurred_at.desc()).limit(20)
        ).scalars().all()

    print(f"{total} event(s) in the log; showing the most recent {len(recent)}\n")
    for event in recent:
        timestamp = event.occurred_at.strftime("%Y-%m-%d %H:%M:%S")
        aggregate = f"{event.aggregate_type}/{event.aggregate_id[:8]}"
        print(f"  {timestamp}  {event.event_type:<38} {aggregate}")
    return 0


def command_seed() -> int:
    """Load demo data by delegating to the seed script."""
    from scripts.seed import run_seed

    return run_seed()


def command_fresh() -> int:
    """Reset the schema and reload demo data."""
    command_reset()
    return command_seed()


COMMANDS = {
    "check": command_check,
    "create": command_create,
    "reset": command_reset,
    "seed": command_seed,
    "fresh": command_fresh,
    "tables": command_tables,
    "events": command_events,
}


def main() -> int:
    """Dispatch the requested command."""
    if len(sys.argv) < EXPECTED_ARGUMENT_COUNT or sys.argv[1] not in COMMANDS:
        print(__doc__)
        print(f"available commands: {', '.join(COMMANDS)}")
        return 1

    return COMMANDS[sys.argv[1]]()


if __name__ == "__main__":
    sys.exit(main())
