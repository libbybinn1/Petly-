"""Embed every discoverable adopter profile into the profile vector index.

Lecturer requirement 6: adopter profiles ("résumés") are embedded in the
vector database so a semantic search can match adopters to an animal. The
agent keeps the index in sync on every `SEMANTIC_ADOPTER_SEARCH` job and
re-embeds only profiles whose text changed, so this script is a pre-build:
run it once after seeding, and the first search a staff member starts does
not have to embed the whole population while they wait.

Reads the database, writes only the local Chroma directory. Idempotent:
running it twice embeds nothing the second time.

Usage:
    C:/Users/libbyb/venvs/petmatch/Scripts/python.exe scripts/ingest_profiles.py
    C:/Users/libbyb/venvs/petmatch/Scripts/python.exe scripts/ingest_profiles.py --rebuild
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_service.rag.profile_population import discoverable_profile_documents  # noqa: E402
from agent_service.worker import build_profile_index  # noqa: E402
from app.config import load_configuration  # noqa: E402
from app.infrastructure.database import (  # noqa: E402
    create_database_engine,
    create_session_factory,
)

REBUILD_FLAG = "--rebuild"


def main() -> int:
    """Sync the profile index with the database and report what changed.

    `--rebuild` drops the collection first, which forces every résumé to be
    embedded again - needed only after changing the embedding model by hand
    in a way the content hash cannot see.
    """
    configuration = load_configuration()
    profile_index = build_profile_index(configuration)

    if REBUILD_FLAG in sys.argv[1:]:
        profile_index.clear()
        print(f"cleared '{configuration.profile_collection_name}'")

    session_factory = create_session_factory(create_database_engine(configuration))
    with session_factory() as session:
        documents = discoverable_profile_documents(session)

    print(
        f"syncing {len(documents)} discoverable profile(s) "
        f"with {configuration.agent.embedding_model}..."
    )
    report = profile_index.sync(documents)

    print(
        f"embedded {report.embedded_count}, unchanged {report.unchanged_count}, "
        f"removed {report.removed_count}"
    )
    print(
        f"'{configuration.profile_collection_name}' now holds {profile_index.count()} "
        f"profile(s), persisted to {configuration.chroma_persist_directory}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
