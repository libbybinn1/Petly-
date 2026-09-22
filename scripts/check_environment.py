"""Verify every external dependency PetMatch relies on is reachable and working.

Run this after cloning, after moving the project, or when something external
starts failing. It checks each integration independently and reports which ones
are usable, so a single broken service does not look like a broken project.

Usage:
    .venv/Scripts/python.exe scripts/check_environment.py
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

import truststore

# The corporate network (Palo Alto "Forward Trust CA") re-signs HTTPS traffic.
# Python's bundled CA list does not contain that root, so every outbound HTTPS
# call fails certificate verification unless we use the Windows trust store.
# This must run before any HTTPS client is constructed.
truststore.inject_into_ssl()

from dotenv import load_dotenv  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(dotenv_path=PROJECT_ROOT / ".env")


@dataclass
class CheckResult:
    """Outcome of a single environment check."""

    name: str
    passed: bool
    detail: str


def check_database() -> CheckResult:
    """Confirm the Somee SQL Server accepts a login and reports its version."""
    import pymssql

    try:
        connection = pymssql.connect(
            server=os.environ["DB_SERVER"],
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"],
            database=os.environ["DB_NAME"],
            timeout=30,
            login_timeout=30,
        )
    except Exception as error:  # - report any failure to the user
        return CheckResult("Cloud database", False, f"{type(error).__name__}: {error}")

    with connection:
        cursor = connection.cursor()
        cursor.execute("SELECT @@VERSION")
        version_line = str(cursor.fetchone()[0]).splitlines()[0].strip()
        cursor.execute("SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES")
        table_count = cursor.fetchone()[0]

    return CheckResult("Cloud database", True, f"{version_line} | {table_count} tables")


def check_ollama_chat() -> CheckResult:
    """Confirm the chat model responds and honours JSON output mode."""
    import json

    import ollama

    model_name = os.environ["OLLAMA_CHAT_MODEL"]
    try:
        client = ollama.Client(host=os.environ["OLLAMA_BASE_URL"])
        response = client.chat(
            model=model_name,
            messages=[{"role": "user", "content": 'Reply with JSON {"ok": true}'}],
            format="json",
            options={"temperature": 0},
        )
        json.loads(response["message"]["content"])
    except Exception as error:
        return CheckResult("Ollama chat", False, f"{type(error).__name__}: {error}")

    return CheckResult("Ollama chat", True, f"{model_name} responded with valid JSON")


def check_ollama_embeddings() -> CheckResult:
    """Confirm the embedding model returns a vector of the expected width."""
    import ollama

    model_name = os.environ["OLLAMA_EMBED_MODEL"]
    try:
        client = ollama.Client(host=os.environ["OLLAMA_BASE_URL"])
        vector = client.embeddings(model=model_name, prompt="a calm indoor rabbit")["embedding"]
    except Exception as error:
        return CheckResult("Ollama embeddings", False, f"{type(error).__name__}: {error}")

    if not vector:
        return CheckResult("Ollama embeddings", False, "model returned an empty vector")

    return CheckResult("Ollama embeddings", True, f"{model_name} -> {len(vector)} dimensions")


def check_vector_database() -> CheckResult:
    """Confirm ChromaDB stores and retrieves vectors produced by our embedding model.

    Embeddings are always supplied explicitly. If they are omitted, ChromaDB
    silently falls back to its own bundled ONNX model and downloads ~79 MB -
    which would defeat the choice of nomic-embed-text and add a second,
    inconsistent embedding space. The real ingestion pipeline has the same rule.
    """
    import chromadb
    import ollama

    embed_model = os.environ["OLLAMA_EMBED_MODEL"]
    ollama_client = ollama.Client(host=os.environ["OLLAMA_BASE_URL"])

    def embed(text: str) -> list[float]:
        return list(ollama_client.embeddings(model=embed_model, prompt=text)["embedding"])

    try:
        collection = chromadb.EphemeralClient().create_collection("environment_check")
        document = "rabbits are quiet indoor companions suited to small homes"
        collection.add(ids=["a"], documents=[document], embeddings=[embed(document)])
        results = collection.query(query_embeddings=[embed("calm small pet")], n_results=1)
    except Exception as error:
        return CheckResult("Vector database", False, f"{type(error).__name__}: {error}")

    if not results["ids"][0]:
        return CheckResult("Vector database", False, "query returned no results")

    return CheckResult(
        "Vector database", True, f"ChromaDB {chromadb.__version__} stored and retrieved a vector"
    )


def check_web_search() -> CheckResult:
    """Confirm Tavily authenticates and returns results through the corporate proxy."""
    from tavily import TavilyClient

    api_key = os.environ.get("TAVILY_API_KEY", "")
    if not api_key:
        return CheckResult("Web search", False, "TAVILY_API_KEY is not set in .env")

    try:
        response = TavilyClient(api_key=api_key).search(query="dog adoption", max_results=1)
    except Exception as error:
        return CheckResult("Web search", False, f"{type(error).__name__}: {error}")

    result_count = len(response.get("results", []))
    return CheckResult("Web search", True, f"Tavily returned {result_count} result(s)")


ALL_CHECKS = (
    check_database,
    check_ollama_chat,
    check_ollama_embeddings,
    check_vector_database,
    check_web_search,
)


def main() -> int:
    """Run every check and return a non-zero exit code if any failed."""
    print(f"PetMatch environment check\nproject root: {PROJECT_ROOT}\n")

    results = [check() for check in ALL_CHECKS]

    for result in results:
        marker = "PASS" if result.passed else "FAIL"
        print(f"  [{marker}] {result.name:<20} {result.detail}")

    failed_count = sum(1 for result in results if not result.passed)
    print(f"\n{len(results) - failed_count}/{len(results)} checks passed")
    return 1 if failed_count else 0


if __name__ == "__main__":
    sys.exit(main())
