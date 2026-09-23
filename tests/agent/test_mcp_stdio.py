"""Tests for the local MCP tool server (course blueprint section 8).

These tests spawn the **real server as a subprocess** and talk to it over
stdio. The transport is deliberately not mocked: stdio communication is the
thing the requirement is about, so mocking it would leave the requirement
untested.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from app.config import load_configuration
from app.infrastructure.database import create_database_engine, create_session_factory
from app.infrastructure.models import AdopterProfile, Animal
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, TextContent
from sqlalchemy import select

pytestmark = [pytest.mark.agent, pytest.mark.anyio]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PYTHON_EXECUTABLE = sys.executable

EXPECTED_TOOL_NAMES = {"get_adopter_profile", "get_animal_profile"}


@pytest.fixture
def anyio_backend() -> str:
    """Run the async tests on asyncio only."""
    return "asyncio"


@pytest.fixture(scope="module")
def known_identifiers() -> dict[str, str]:
    """Fetch one real adopter and animal id from the seeded database.

    Reading real identifiers keeps the test honest: it proves the server
    returns actual records rather than echoing whatever it was handed.
    """
    session_factory = create_session_factory(create_database_engine(load_configuration()))
    with session_factory() as session:
        adopter = session.execute(select(AdopterProfile).limit(1)).scalar_one_or_none()
        animal = session.execute(select(Animal).limit(1)).scalar_one_or_none()

    if adopter is None or animal is None:
        pytest.skip("database has no seed data; run scripts/db.py fresh")

    return {
        "adopter_profile_id": adopter.adopter_profile_id,
        "animal_id": animal.animal_id,
        "animal_name": animal.name,
    }


def _server_parameters() -> StdioServerParameters:
    """Describe how to launch the tool server as a subprocess."""
    return StdioServerParameters(
        command=PYTHON_EXECUTABLE,
        args=["-m", "mcp_server"],
        cwd=str(PROJECT_ROOT),
    )


def _payload_of(result: CallToolResult) -> dict[str, Any]:
    """Extract the JSON payload from an MCP tool result.

    Asserts the reply is a single text block rather than assuming it: that
    the tools answer with JSON text is part of the contract these tests
    exist to check.
    """
    content = result.content[0]
    assert isinstance(content, TextContent), "tools must answer with text"
    payload = json.loads(content.text)
    assert isinstance(payload, dict), "tools must answer with a JSON object"
    return payload


async def test_server_advertises_both_tools() -> None:
    """Proves both required stdio tools exist and carry LLM-facing descriptions.

    Blueprint section 8 requires at least two local tools, each with a
    description explaining to a model what it can do.
    """
    async with (
        stdio_client(_server_parameters()) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        listing = await session.list_tools()

    advertised = {tool.name for tool in listing.tools}
    assert advertised >= EXPECTED_TOOL_NAMES

    for tool in listing.tools:
        if tool.name in EXPECTED_TOOL_NAMES:
            assert tool.description, f"{tool.name} has no description for the model"
            assert len(tool.description) > 80, (
                f"{tool.name} description is too thin to guide a model"
            )


async def test_get_adopter_profile_round_trips(known_identifiers: dict[str, str]) -> None:
    """Proves a real adopter record travels back over stdio intact."""
    async with (
        stdio_client(_server_parameters()) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        result = await session.call_tool(
            "get_adopter_profile",
            {"adopter_profile_id": known_identifiers["adopter_profile_id"]},
        )

    payload = _payload_of(result)
    assert payload["found"] is True
    assert payload["adopter_profile_id"] == known_identifiers["adopter_profile_id"]

    # The fields the matching engine actually consumes must all be present.
    for required_field in (
        "home_type",
        "has_yard",
        "household_has_children",
        "has_other_animals",
        "experience_level",
        "activity_level",
        "daily_hours_available",
        "city",
        "open_to_proactive_suggestions",
        "is_complete",
    ):
        assert required_field in payload, f"missing {required_field}"


async def test_get_animal_profile_round_trips(known_identifiers: dict[str, str]) -> None:
    """Proves a real animal record travels back over stdio intact."""
    async with (
        stdio_client(_server_parameters()) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        result = await session.call_tool(
            "get_animal_profile", {"animal_id": known_identifiers["animal_id"]}
        )

    payload = _payload_of(result)
    assert payload["found"] is True
    assert payload["animal_id"] == known_identifiers["animal_id"]
    assert payload["name"] == known_identifiers["animal_name"]

    for required_field in (
        "species",
        "age_years",
        "size",
        "temperament",
        "activity_level",
        "good_with_children",
        "good_with_other_animals",
        "has_special_needs",
        "required_space",
        "status",
    ):
        assert required_field in payload, f"missing {required_field}"


async def test_unknown_identifier_returns_structured_not_found() -> None:
    """Proves a missing record yields a usable answer, not a protocol error.

    An exception crossing the MCP boundary reaches the model as an opaque
    failure. A structured `found: false` is something the agent can act on
    and report under `missing_information`.
    """
    async with (
        stdio_client(_server_parameters()) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        result = await session.call_tool(
            "get_animal_profile", {"animal_id": "00000000-0000-0000-0000-000000000000"}
        )

    payload = _payload_of(result)
    assert payload["found"] is False
    assert payload["reason"]


async def test_tools_expose_no_mutating_operation() -> None:
    """Proves the tool surface is read-only.

    A tool that could write would let the agent change the system it is
    supposed to be advising about.
    """
    forbidden_verbs = ("create", "update", "delete", "set_", "approve", "send", "write")

    async with (
        stdio_client(_server_parameters()) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        listing = await session.list_tools()

    for tool in listing.tools:
        lowered = tool.name.lower()
        assert not any(verb in lowered for verb in forbidden_verbs), (
            f"{tool.name} looks like a mutating tool"
        )
