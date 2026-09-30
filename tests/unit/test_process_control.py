"""Tests for the process discovery that `start_all.py` and `stop_all.py` rely on.

These scripts kill processes. The interesting property is therefore not that
they find PetMatch -- it is that they find *nothing else*. This machine really
does run unrelated `run.py` and `main.py` scripts belonging to other projects,
and a shutdown helper that matched on the filename alone would terminate a
colleague's work along with its own.

The classification is pure string logic, so it is tested here with no processes
started, nothing killed and no PowerShell call.
"""

from __future__ import annotations

import pytest
from scripts import process_control
from scripts.process_control import ProcessRole

pytestmark = pytest.mark.unit

PROJECT_DIRECTORY = str(process_control.PROJECT_ROOT)
VIRTUALENV_DIRECTORY = str(process_control.VIRTUALENV_SCRIPTS_DIRECTORY)


def test_web_application_launched_from_the_project_venv_is_recognised() -> None:
    """Proves a run.py started with the project interpreter classifies as the web app.

    This is the command line `start_all.py` itself produces, so if this fails
    the two scripts cannot see the processes they just launched.
    """
    command_line = f"{VIRTUALENV_DIRECTORY}\\python.exe run.py"
    assert process_control._classify(command_line) is ProcessRole.WEB_APPLICATION


def test_agent_worker_launched_as_a_module_is_recognised() -> None:
    """Proves `-m agent_service` classifies as the agent worker.

    The agent is a separate OS process by design (CLAUDE.md R2), so it has to
    be discovered separately from the web application rather than assumed dead
    when the web application stops.
    """
    command_line = f"{VIRTUALENV_DIRECTORY}\\python.exe -m agent_service"
    assert process_control._classify(command_line) is ProcessRole.AGENT_WORKER


def test_a_run_py_from_a_different_project_is_never_claimed() -> None:
    """Proves a foreign run.py is not classified as PetMatch's. The safety property.

    Matching on the filename alone would make `stop_all.py` terminate any
    unrelated `run.py` on the machine. Requiring the project directory or the
    project virtual environment on the command line is what prevents that.
    """
    foreign = r"C:\Projects\SomeoneElse\.venv\Scripts\python.exe run.py"
    assert process_control._classify(foreign) is None


def test_an_unrelated_python_process_in_the_project_is_not_claimed() -> None:
    """Proves living in the project directory is not on its own enough to be killed.

    A pytest run or a seeding script started from this checkout matches the
    directory test but is not a PetMatch server, and must survive `stop_all.py`.
    """
    command_line = f"{VIRTUALENV_DIRECTORY}\\python.exe -m pytest tests/unit"
    assert process_control._classify(command_line) is None


def test_paths_match_regardless_of_slash_direction_and_case() -> None:
    """Proves discovery survives Windows reporting the same path several ways.

    Windows hands back forward slashes, back slashes and varying case for one
    directory. A literal comparison would miss a real process and leave it
    running, which is the orphan problem these scripts exist to solve.
    """
    command_line = f"{PROJECT_DIRECTORY.replace(chr(92), '/').upper()}/run.py"
    assert process_control._classify(command_line) is ProcessRole.WEB_APPLICATION


def test_an_empty_command_line_is_not_claimed() -> None:
    """Proves a process whose command line could not be read is left alone.

    Windows reports a null command line for processes this user may not
    inspect. Those are never ours, and guessing would mean killing blind.
    """
    assert process_control._classify("") is None


def test_a_row_without_a_usable_process_id_is_discarded() -> None:
    """Proves malformed PowerShell rows are dropped rather than becoming process id 0.

    Terminating process id 0 is meaningless at best, so a row that lost its
    ProcessId must not survive parsing.
    """
    assert process_control._read_entry({"ProcessId": None, "CommandLine": "x"}) is None
    assert process_control._read_entry({"CommandLine": "x"}) is None
    assert process_control._read_entry({"ProcessId": 0, "CommandLine": "x"}) is None


def test_a_row_with_a_null_command_line_keeps_its_process_id() -> None:
    """Proves an unreadable command line yields an empty string, not a crash.

    The row is still parsed so the caller can decide; `_classify` then declines
    it. Raising here would break enumeration for every other process too.
    """
    row = process_control._read_entry({"ProcessId": 42, "CommandLine": None})
    assert row is not None
    assert (row.process_id, row.command_line) == (42, "")


def test_a_child_of_a_matched_process_inherits_its_role() -> None:
    """Proves the base-interpreter child of run.py is claimed alongside its parent.

    Launching `run.py` yields two processes: the venv interpreter and a child
    re-executed from the base Python installation. The child's command line
    carries no project path, so only parentage finds it. Missing it leaves a
    process holding the port after its parent is killed -- the exact way
    orphans accumulated on this machine.
    """
    rows = [
        process_control._ProcessTableRow(100, 1, f"{VIRTUALENV_DIRECTORY}\\python.exe run.py"),
        process_control._ProcessTableRow(101, 100, r"C:\Python312\python.exe run.py"),
    ]
    roles = {100: ProcessRole.WEB_APPLICATION}

    process_control._claim_descendants(rows, roles)

    assert roles[101] is ProcessRole.WEB_APPLICATION


def test_a_grandchild_is_claimed_even_when_listed_before_its_parent() -> None:
    """Proves claiming repeats until closed, so table ordering cannot hide a descendant.

    Windows does not return the process table parent-first. A single pass would
    skip a grandchild listed early and leave it running.
    """
    rows = [
        process_control._ProcessTableRow(102, 101, r"C:\Python312\python.exe run.py"),
        process_control._ProcessTableRow(101, 100, r"C:\Python312\python.exe run.py"),
    ]
    roles = {100: ProcessRole.AGENT_WORKER}

    process_control._claim_descendants(rows, roles)

    assert roles[101] is ProcessRole.AGENT_WORKER
    assert roles[102] is ProcessRole.AGENT_WORKER


def test_an_unrelated_process_is_not_claimed_by_descent() -> None:
    """Proves descent only spreads from an already-matched process. The safety property.

    Every Python process has a parent. If parentage were followed without a
    matched starting point, `stop_all.py` would walk out into unrelated work.
    """
    rows = [
        process_control._ProcessTableRow(200, 1, r"C:\Other\python.exe run.py"),
        process_control._ProcessTableRow(201, 200, r"C:\Other\python.exe worker.py"),
    ]
    roles: dict[int, ProcessRole] = {}

    process_control._claim_descendants(rows, roles)

    assert roles == {}
