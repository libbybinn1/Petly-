"""RAG knowledge base: ingestion, embedding and semantic retrieval.

Course blueprint section 7 requires relevant information to be embedded in a
vector database and retrieved *semantically* - keyword search alone is not
sufficient. This module implements that pipeline:

    knowledge/*.md
      -> chunk by markdown section
      -> embed with nomic-embed-text via Ollama
      -> store in ChromaDB, persisted to disk
      -> retrieve top-k by vector similarity

The vector store holds the project's **curated knowledge only**. Adopter and
animal records live in SQL Server and reach the agent through MCP tools
(blueprint section 11). Nothing transactional is embedded here.

Embeddings are always supplied explicitly. If they are omitted ChromaDB
silently downloads its own ONNX model and builds a second, inconsistent
embedding space - so every call in this module passes vectors.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import chromadb
import ollama
from chromadb.api.models.Collection import Collection
from chromadb.api.types import QueryResult

logger = logging.getLogger("petmatch.agent.rag")

# Sections longer than this are split further, so a single retrieved chunk
# stays small enough to be useful as evidence rather than a wall of text.
MAX_CHUNK_CHARACTERS = 1400
CHUNK_OVERLAP_CHARACTERS = 160

# Chroma returns squared L2 distances for these embeddings. Anything beyond
# this is unrelated in practice, and admitting it would let the agent cite
# irrelevant material as evidence.
DEFAULT_RELEVANCE_THRESHOLD = 420.0
DEFAULT_RESULT_COUNT = 4


@dataclass(frozen=True)
class KnowledgeChunk:
    """One retrievable passage of curated guidance."""

    chunk_id: str
    document_name: str
    heading: str
    text: str

    @property
    def citation(self) -> str:
        """A short reference the agent can quote as its source."""
        anchor = re.sub(r"[^a-z0-9]+", "-", self.heading.lower()).strip("-")
        return f"{self.document_name}#{anchor}"


@dataclass(frozen=True)
class RetrievedChunk:
    """A chunk returned by a search, with its distance from the query."""

    chunk: KnowledgeChunk
    distance: float

    @property
    def is_relevant(self) -> bool:
        """Whether this result is close enough to cite."""
        return self.distance <= DEFAULT_RELEVANCE_THRESHOLD


class EmbeddingClient:
    """Turns text into vectors using the local embedding model.

    Wrapped in a class so the agent and the ingestion script share one
    implementation, and so tests can substitute a deterministic stand-in.
    """

    def __init__(self, base_url: str, model_name: str) -> None:
        """Bind the client to an Ollama host and model."""
        self._client = ollama.Client(host=base_url)
        self._model_name = model_name

    def embed(self, text: str) -> list[float]:
        """Embed a single passage."""
        response = self._client.embeddings(model=self._model_name, prompt=text)
        return list(response["embedding"])

    def embed_all(self, texts: list[str]) -> list[list[float]]:
        """Embed several passages in order."""
        return [self.embed(text) for text in texts]


def split_markdown_into_chunks(document_path: Path) -> list[KnowledgeChunk]:
    """Split one markdown guide into retrievable chunks.

    Chunking follows the document's own `##` headings rather than a fixed
    character window, because each section already covers one topic. A
    heading-shaped chunk retrieves more cleanly and gives the agent a
    meaningful citation anchor.

    Args:
        document_path: Path to a markdown file under `knowledge/`.

    Returns:
        Chunks in document order.
    """
    raw_text = document_path.read_text(encoding="utf-8")
    document_name = document_path.name

    sections = _split_into_headed_sections(raw_text)

    chunks: list[KnowledgeChunk] = []
    for section_index, (heading, body) in enumerate(sections):
        for part_index, part in enumerate(_split_long_text(body)):
            chunks.append(
                KnowledgeChunk(
                    chunk_id=f"{document_name}::{section_index}::{part_index}",
                    document_name=document_name,
                    heading=heading,
                    text=f"{heading}\n\n{part}".strip(),
                )
            )
    return chunks


def _split_into_headed_sections(raw_text: str) -> list[tuple[str, str]]:
    """Break markdown into (heading, body) pairs on `##` boundaries."""
    lines = raw_text.splitlines()
    document_title = ""
    sections: list[tuple[str, str]] = []
    current_heading = ""
    current_body: list[str] = []

    for line in lines:
        if line.startswith("# "):
            document_title = line[2:].strip()
            continue
        if line.startswith("## "):
            if current_heading and current_body:
                sections.append((current_heading, "\n".join(current_body).strip()))
            current_heading = f"{document_title}: {line[3:].strip()}"
            current_body = []
            continue
        current_body.append(line)

    if current_heading and current_body:
        sections.append((current_heading, "\n".join(current_body).strip()))

    return [(heading, body) for heading, body in sections if body]


def _split_long_text(text: str) -> list[str]:
    """Split an over-long section into overlapping windows.

    Overlap keeps a sentence that straddles a boundary retrievable from
    either side.
    """
    if len(text) <= MAX_CHUNK_CHARACTERS:
        return [text]

    parts: list[str] = []
    start = 0
    while start < len(text):
        end = start + MAX_CHUNK_CHARACTERS
        parts.append(text[start:end].strip())
        start = end - CHUNK_OVERLAP_CHARACTERS
    return [part for part in parts if part]


class KnowledgeRetriever(Protocol):
    """The retrieval surface the agent actually depends on.

    The agent needs one method, so it asks for one method. Depending on the
    concrete `KnowledgeBase` would mean depending on Chroma, an embedding
    client and a directory of markdown - none of which an agent test should
    have to stand up to check how a retrieved chunk is used.
    """

    def search_relevant(
        self, query: str, result_count: int = DEFAULT_RESULT_COUNT
    ) -> list[RetrievedChunk]:
        """Return the passages relevant enough to cite."""
        ...


class KnowledgeBase:
    """Semantic search over the curated adoption knowledge."""

    def __init__(
        self,
        persist_directory: Path,
        collection_name: str,
        embedding_client: EmbeddingClient,
    ) -> None:
        """Open, or create, the persistent vector collection."""
        persist_directory.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(persist_directory))
        self._collection_name = collection_name
        self._embedding_client = embedding_client

    @property
    def _collection(self) -> Collection:
        """The Chroma collection, created on first use."""
        return self._client.get_or_create_collection(name=self._collection_name)

    def ingest_directory(self, knowledge_directory: Path) -> int:
        """Embed and store every markdown guide in a directory.

        Existing content is cleared first, so re-running ingestion after
        editing a guide replaces it rather than leaving stale duplicates.

        Args:
            knowledge_directory: Directory holding the markdown guides.

        Returns:
            The number of chunks stored.
        """
        document_paths = sorted(knowledge_directory.glob("*.md"))
        if not document_paths:
            return 0

        self.clear()

        all_chunks: list[KnowledgeChunk] = []
        for path in document_paths:
            all_chunks.extend(split_markdown_into_chunks(path))

        if not all_chunks:
            return 0

        # Declared with Chroma's own wider element type: `list` is
        # invariant, so a plain list[list[float]] is not accepted where a
        # list of sequences is expected.
        vectors: list[Sequence[float] | Sequence[int]] = list(
            self._embedding_client.embed_all([chunk.text for chunk in all_chunks])
        )

        self._collection.add(
            ids=[chunk.chunk_id for chunk in all_chunks],
            documents=[chunk.text for chunk in all_chunks],
            embeddings=vectors,
            metadatas=[
                {"document_name": chunk.document_name, "heading": chunk.heading}
                for chunk in all_chunks
            ],
        )
        return len(all_chunks)

    def search(
        self, query: str, result_count: int = DEFAULT_RESULT_COUNT
    ) -> list[RetrievedChunk]:
        """Find the passages most semantically similar to a question.

        Retrieval is by vector similarity, not keyword overlap, so a question
        phrased in entirely different words still finds the right guidance -
        which is what blueprint section 7 requires.

        Args:
            query: A natural-language question.
            result_count: Maximum results to return.

        Returns:
            Results ordered nearest first, or an empty list when retrieval
            is impossible. Callers should check `is_relevant` before citing
            one.
        """
        # A blank query embeds to an empty vector, which Chroma rejects. An
        # adopter submitting an empty natural-language search must not crash
        # the agent, so answer "no evidence" instead.
        if not query.strip():
            return []

        try:
            response = self._nearest_neighbours(query, result_count)
        except Exception as error:  # - an outage degrades to "no evidence"
            # Embedding runs through Ollama, so a stopped model host, a
            # pulled embedding model or a corrupt store all surface here.
            # Raising would fail the analysis job outright and falsify the
            # documented behaviour: a model outage must cost the prose, not
            # the score (docs/AGENT.md section 10).
            logger.warning("knowledge base retrieval failed: %s", error)
            return []

        if response is None:
            return []

        # Every field of a Chroma result is optional, because a caller can
        # ask for a subset. We ask for the default set, which includes all
        # four - but a missing one would mean a silent contract change, so
        # answer "no evidence" rather than indexing into None.
        identifiers = response["ids"]
        documents = response["documents"]
        metadatas = response["metadatas"]
        distances = response["distances"]
        if documents is None or metadatas is None or distances is None:
            return []

        return [
            RetrievedChunk(
                chunk=KnowledgeChunk(
                    chunk_id=chunk_id,
                    document_name=str(metadata.get("document_name", "")),
                    heading=str(metadata.get("heading", "")),
                    text=document,
                ),
                distance=float(distance),
            )
            for chunk_id, document, metadata, distance in zip(
                identifiers[0],
                documents[0],
                metadatas[0],
                distances[0],
                strict=True,
            )
        ]

    def _nearest_neighbours(self, query: str, result_count: int) -> QueryResult | None:
        """Embed one question and ask Chroma for its nearest stored chunks.

        Args:
            query: The question to embed.
            result_count: How many neighbours to ask for.

        Returns:
            Chroma's raw result, or None when nothing is stored yet.
        """
        stored_count = self.count()
        if stored_count == 0:
            return None

        query_vector: list[Sequence[float] | Sequence[int]] = [
            self._embedding_client.embed(query)
        ]
        return self._collection.query(
            query_embeddings=query_vector,
            n_results=min(result_count, stored_count),
        )

    def search_relevant(
        self, query: str, result_count: int = DEFAULT_RESULT_COUNT
    ) -> list[RetrievedChunk]:
        """Search and keep only results close enough to cite as evidence."""
        return [result for result in self.search(query, result_count) if result.is_relevant]

    def count(self) -> int:
        """How many chunks are stored."""
        return int(self._collection.count())

    def clear(self) -> None:
        """Remove every stored chunk.

        Tolerates a collection that does not exist yet, so a first ingestion
        into an empty store behaves the same as a re-ingestion.
        """
        existing_names = {collection.name for collection in self._client.list_collections()}
        if self._collection_name not in existing_names:
            return
        self._client.delete_collection(name=self._collection_name)
