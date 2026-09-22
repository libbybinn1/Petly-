"""Tests for the RAG knowledge base (course blueprint section 7).

Blueprint section 7 requires semantic search and states that keyword search
alone is not sufficient. The central test here therefore asks questions that
share **no meaningful vocabulary** with the passage they should retrieve. A
keyword index would fail those outright.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agent_service.rag.knowledge_base import (
    EmbeddingClient,
    KnowledgeBase,
    split_markdown_into_chunks,
)
from app.config import load_configuration

pytestmark = pytest.mark.agent

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_DIRECTORY = PROJECT_ROOT / "knowledge"

# Words too common to count as a meaningful keyword overlap.
STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do", "for",
    "from", "has", "have", "how", "i", "if", "in", "is", "it", "its", "my",
    "no", "not", "of", "on", "or", "our", "should", "so", "that", "the",
    "their", "them", "they", "this", "to", "up", "was", "we", "what", "when",
    "which", "who", "will", "with", "would", "you", "your", "am", "much",
    "very", "more", "most", "need", "needs", "get", "got", "want", "wants",
    "there", "here", "about", "into", "than", "then", "some", "any", "all",
}


@pytest.fixture(scope="module")
def knowledge_base() -> KnowledgeBase:
    """Open the persisted knowledge base, skipping if it was never ingested."""
    configuration = load_configuration()
    base = KnowledgeBase(
        persist_directory=configuration.chroma_persist_directory,
        collection_name=configuration.rag_collection_name,
        embedding_client=EmbeddingClient(
            base_url=configuration.agent.ollama_base_url,
            model_name=configuration.agent.embedding_model,
        ),
    )
    if base.count() == 0:
        pytest.skip("knowledge base is empty; run scripts/ingest_knowledge.py")
    return base


def meaningful_words(text: str) -> set[str]:
    """Lowercase content words, with punctuation and stop words removed."""
    cleaned = "".join(character if character.isalnum() else " " for character in text.lower())
    return {word for word in cleaned.split() if word not in STOP_WORDS and len(word) > 2}


class TestChunking:
    """Chunks are built from the documents' own structure."""

    def test_every_guide_produces_chunks(self) -> None:
        """Proves no guide is silently skipped during ingestion."""
        for document_path in sorted(KNOWLEDGE_DIRECTORY.glob("*.md")):
            chunks = split_markdown_into_chunks(document_path)
            assert chunks, f"{document_path.name} produced no chunks"

    def test_chunks_carry_a_citable_reference(self) -> None:
        """Proves the agent can cite a specific section, not just a file."""
        chunks = split_markdown_into_chunks(KNOWLEDGE_DIRECTORY / "space-and-housing.md")

        for chunk in chunks:
            assert chunk.citation.endswith(tuple("abcdefghijklmnopqrstuvwxyz0123456789"))
            assert "#" in chunk.citation
            assert chunk.document_name == "space-and-housing.md"

    def test_chunk_ids_are_unique(self) -> None:
        """Proves ingestion cannot silently overwrite one chunk with another."""
        all_ids = [
            chunk.chunk_id
            for path in sorted(KNOWLEDGE_DIRECTORY.glob("*.md"))
            for chunk in split_markdown_into_chunks(path)
        ]
        assert len(all_ids) == len(set(all_ids))


class TestSemanticRetrieval:
    """Retrieval must work on meaning, not shared words."""

    def test_stored_chunk_count_matches_the_corpus(self, knowledge_base: KnowledgeBase) -> None:
        """Proves ingestion stored the whole corpus."""
        expected = sum(
            len(split_markdown_into_chunks(path))
            for path in sorted(KNOWLEDGE_DIRECTORY.glob("*.md"))
        )
        assert knowledge_base.count() == expected

    @pytest.mark.parametrize(
        ("question", "expected_document"),
        [
            (
                "I am out of the house all day for work. Is that a problem?",
                "activity-and-exercise.md",
            ),
            (
                "We have a toddler at home. What should we be careful about?",
                "children-and-households.md",
            ),
            (
                "Our flat is tiny and there is nowhere outside. What fits?",
                "space-and-housing.md",
            ),
            (
                "The animal is quite old now. What extra care is involved?",
                "senior-and-special-needs.md",
            ),
            (
                "We already have a cat at home. Will that cause trouble?",
                "multi-pet-households.md",
            ),
        ],
    )
    def test_question_retrieves_the_right_guide(
        self, knowledge_base: KnowledgeBase, question: str, expected_document: str
    ) -> None:
        """Proves a plainly-worded question reaches the correct guidance.

        None of these questions quote the documents. They are phrased the way
        an adopter would actually ask.
        """
        results = knowledge_base.search(question, result_count=3)

        assert results, "retrieval returned nothing"
        retrieved_documents = {result.chunk.document_name for result in results}
        assert expected_document in retrieved_documents, (
            f"expected {expected_document}, got {sorted(retrieved_documents)}"
        )

    def test_retrieval_succeeds_with_no_keyword_overlap(
        self, knowledge_base: KnowledgeBase
    ) -> None:
        """Proves retrieval is semantic, which blueprint section 7 demands.

        The question shares no meaningful word with the passage it should
        find. A keyword index would return nothing useful here.
        """
        question = "Nobody is home between morning and evening."

        results = knowledge_base.search(question, result_count=3)
        assert results

        best = results[0]
        overlap = meaningful_words(question) & meaningful_words(best.chunk.text)

        assert not overlap, f"test is invalid - question shares words {overlap}"
        assert best.is_relevant

    def test_results_are_ordered_by_similarity(self, knowledge_base: KnowledgeBase) -> None:
        """Proves the nearest passage is returned first."""
        results = knowledge_base.search("How much exercise does a working dog need?", 4)

        distances = [result.distance for result in results]
        assert distances == sorted(distances)

    def test_unrelated_question_yields_no_relevant_evidence(
        self, knowledge_base: KnowledgeBase
    ) -> None:
        """Proves the relevance threshold filters out unrelated matches.

        Without this the agent would happily cite pet-care guidance as
        evidence for a question about something else entirely. Chroma always
        returns its nearest neighbours, however far away they are.
        """
        results = knowledge_base.search_relevant(
            "What is the capital of France and when was the Eiffel Tower built?"
        )
        assert results == []

    def test_search_on_empty_query_does_not_raise(
        self, knowledge_base: KnowledgeBase
    ) -> None:
        """Proves a blank query degrades gracefully rather than crashing the agent."""
        assert isinstance(knowledge_base.search("", result_count=1), list)
