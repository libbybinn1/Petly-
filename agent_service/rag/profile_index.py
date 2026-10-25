"""Vector index of adopter profile résumés, for semantic adopter search.

Lecturer requirement 6: résumés are embedded in a vector database, and a
semantic search matches a candidate to a job. Here the résumé is an adopter
profile written as plain language by `app.domain.semantic_profiles`, the job
is an animal, and the search runs in this process only - the web tier never
touches Chroma or an embedding model (rule R4: no model call in a request).

    discoverable adopter profiles (SQL Server)
      -> build_adopter_profile_text          (app/domain, deterministic)
      -> content hash; skip if unchanged      (no re-embedding)
      -> embed with nomic-embed-text via Ollama
      -> ChromaDB collection, cosine space, separate from the knowledge base
      -> query with the animal's own description text

Deliberately a **separate collection** from the curated knowledge base. That
one is evidence the agent cites; this one is a search index over records the
SQL database remains authoritative for. Every entry is keyed by its
`adopter_profile_id`, and a profile that leaves the discoverable population -
an adopter who opts out - is deleted on the next sync, so the index never
outlives the consent it was built under.

As in `knowledge_base.py`, embeddings are always passed explicitly. If they
are omitted ChromaDB downloads its own ONNX model and builds a second,
inconsistent embedding space.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import chromadb
from app.domain.semantic_profiles import SemanticAdopterMatch, profile_content_hash
from chromadb.api.models.Collection import Collection
from chromadb.api.types import Metadata

logger = logging.getLogger("petmatch.agent.profiles")

# Cosine distance makes `1 - distance` a cosine similarity, which is
# independent of vector length and reads naturally on screen. The knowledge
# base keeps Chroma's default squared-L2 space; the two never share vectors.
COLLECTION_METADATA: dict[str, Any] = {"hnsw:space": "cosine"}

CONTENT_HASH_KEY = "content_hash"
PROFILE_ID_KEY = "adopter_profile_id"


class Embedder(Protocol):
    """The embedding surface this index needs.

    `agent_service.rag.knowledge_base.EmbeddingClient` satisfies it, and a
    test substitutes a deterministic stand-in with no Ollama behind it.
    """

    def embed(self, text: str) -> list[float]:
        """Embed one passage."""
        ...

    def embed_all(self, texts: list[str]) -> list[list[float]]:
        """Embed several passages, in order."""
        ...


@dataclass(frozen=True)
class ProfileDocument:
    """One adopter résumé, ready to be indexed.

    Attributes:
        adopter_profile_id: The profile, used as the vector's identifier.
        text: The output of `build_adopter_profile_text`.
    """

    adopter_profile_id: str
    text: str


@dataclass(frozen=True)
class IndexSyncReport:
    """What one sync changed.

    Attributes:
        embedded_count: Résumés that were new or changed, and were embedded.
        unchanged_count: Résumés whose stored hash still matched; skipped.
        removed_count: Entries for profiles no longer discoverable; deleted.
    """

    embedded_count: int = 0
    unchanged_count: int = 0
    removed_count: int = 0


class ProfileIndex:
    """A persistent Chroma collection of adopter résumés."""

    def __init__(
        self,
        persist_directory: Path,
        collection_name: str,
        embedding_client: Embedder,
        embedding_model: str,
    ) -> None:
        """Open, or create, the persistent profile collection.

        Args:
            persist_directory: Chroma's on-disk directory, shared with the
                knowledge base; collections are separate inside it.
            collection_name: This index's collection.
            embedding_client: What turns text into vectors.
            embedding_model: The model's name, recorded in each content hash
                so a model change re-embeds every résumé.
        """
        persist_directory.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(persist_directory))
        self._collection_name = collection_name
        self._embedding_client = embedding_client
        self._embedding_model = embedding_model

    @property
    def embedding_model(self) -> str:
        """The embedding model this index's vectors were made with."""
        return self._embedding_model

    @property
    def _collection(self) -> Collection:
        """The Chroma collection, created in cosine space on first use."""
        return self._client.get_or_create_collection(
            name=self._collection_name, metadata=COLLECTION_METADATA
        )

    def count(self) -> int:
        """How many résumés are stored."""
        return int(self._collection.count())

    def sync(self, documents: Sequence[ProfileDocument]) -> IndexSyncReport:
        """Make the index hold exactly these résumés, embedding only changes.

        A résumé whose content hash matches the stored one is skipped: on CPU
        inference each embedding costs real time, and an unchanged profile
        would produce the same vector anyway. Entries not in `documents` are
        deleted, because `documents` is the discoverable population and a
        profile outside it must stop being searchable (spec section 5.1).

        Args:
            documents: Every currently discoverable profile's résumé.

        Returns:
            How many were embedded, skipped and removed.
        """
        desired_hashes = {
            document.adopter_profile_id: profile_content_hash(
                document.text, self._embedding_model
            )
            for document in documents
        }
        stored_hashes = self._stored_hashes()

        changed = [
            document
            for document in documents
            if stored_hashes.get(document.adopter_profile_id)
            != desired_hashes[document.adopter_profile_id]
        ]
        removed_ids = sorted(set(stored_hashes) - set(desired_hashes))

        if changed:
            self._upsert(changed, desired_hashes)
        if removed_ids:
            self._collection.delete(ids=removed_ids)

        report = IndexSyncReport(
            embedded_count=len(changed),
            unchanged_count=len(desired_hashes) - len(changed),
            removed_count=len(removed_ids),
        )
        logger.info(
            "profile index synced: %s embedded, %s unchanged, %s removed",
            report.embedded_count,
            report.unchanged_count,
            report.removed_count,
        )
        return report

    def nearest(
        self,
        query_text: str,
        result_count: int,
        excluded_ids: frozenset[str] = frozenset(),
    ) -> list[SemanticAdopterMatch]:
        """Find the résumés most similar to an animal's query text.

        Unlike knowledge retrieval, an embedding failure here is raised
        rather than swallowed: this search *is* the job, so the worker's
        retry is the right response to a stopped Ollama host, and an empty
        answer would be indistinguishable from "nobody is similar".

        Args:
            query_text: The output of `build_animal_query_text`.
            result_count: How many matches to return at most.
            excluded_ids: Profiles to leave out, such as existing applicants.

        Returns:
            Matches nearest first, with cosine similarity.
        """
        stored_count = self.count()
        if not query_text.strip() or stored_count == 0 or result_count <= 0:
            return []

        # Ask for enough neighbours that excluding some still leaves a full
        # page, without ever asking for more than are stored.
        neighbour_count = min(stored_count, result_count + len(excluded_ids))
        query_vector: list[Sequence[float] | Sequence[int]] = [
            self._embedding_client.embed(query_text)
        ]
        response = self._collection.query(
            query_embeddings=query_vector,
            n_results=neighbour_count,
            include=["metadatas", "distances"],
        )

        identifiers = response["ids"][0]
        distances = response["distances"]
        if distances is None:
            return []

        matches = [
            SemanticAdopterMatch(adopter_profile_id, 1.0 - float(distance))
            for adopter_profile_id, distance in zip(identifiers, distances[0], strict=True)
            if adopter_profile_id not in excluded_ids
        ]
        return matches[:result_count]

    def clear(self) -> None:
        """Remove the whole collection, tolerating one that does not exist."""
        existing_names = {collection.name for collection in self._client.list_collections()}
        if self._collection_name in existing_names:
            self._client.delete_collection(name=self._collection_name)

    def _stored_hashes(self) -> dict[str, str]:
        """Read every stored entry's content hash, keyed by profile."""
        stored = self._collection.get(include=["metadatas"])
        metadatas = stored["metadatas"] or []
        return {
            identifier: str(metadata.get(CONTENT_HASH_KEY, "")) if metadata else ""
            for identifier, metadata in zip(stored["ids"], metadatas, strict=True)
        }

    def _upsert(
        self, documents: Sequence[ProfileDocument], hashes: dict[str, str]
    ) -> None:
        """Embed and store new or changed résumés."""
        texts = [document.text for document in documents]
        vectors: list[Sequence[float] | Sequence[int]] = list(
            self._embedding_client.embed_all(texts)
        )
        metadatas: list[Metadata] = [
            {
                PROFILE_ID_KEY: document.adopter_profile_id,
                CONTENT_HASH_KEY: hashes[document.adopter_profile_id],
            }
            for document in documents
        ]
        self._collection.upsert(
            ids=[document.adopter_profile_id for document in documents],
            documents=texts,
            embeddings=vectors,
            metadatas=metadatas,
        )
