"""Embed the curated knowledge base into the vector database.

Run after adding or editing anything under `knowledge/`. Ingestion clears the
collection first, so edits replace previous content rather than leaving stale
duplicates behind.

Usage:
    .venv/Scripts/python.exe scripts/ingest_knowledge.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_service.rag.knowledge_base import EmbeddingClient, KnowledgeBase  # noqa: E402
from app.config import load_configuration  # noqa: E402


def main() -> int:
    """Ingest every markdown guide and report what was stored."""
    configuration = load_configuration()
    knowledge_directory = PROJECT_ROOT / "knowledge"

    if not knowledge_directory.exists():
        print(f"no knowledge directory at {knowledge_directory}")
        return 1

    embedding_client = EmbeddingClient(
        base_url=configuration.agent.ollama_base_url,
        model_name=configuration.agent.embedding_model,
    )
    knowledge_base = KnowledgeBase(
        persist_directory=configuration.chroma_persist_directory,
        collection_name=configuration.rag_collection_name,
        embedding_client=embedding_client,
    )

    guides = sorted(knowledge_directory.glob("*.md"))
    print(f"ingesting {len(guides)} guide(s) with {configuration.agent.embedding_model}...")

    stored_count = knowledge_base.ingest_directory(knowledge_directory)

    print(f"stored {stored_count} chunks in '{configuration.rag_collection_name}'")
    print(f"persisted to {configuration.chroma_persist_directory}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
