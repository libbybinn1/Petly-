"""Locate and stop the operating-system processes that make up a running PetMatch.

`scripts/start_all.py` must know whether PetMatch is already up before it
launches a second copy, and `scripts/stop_all.py` must know what to shut down.
Both ask the same question, so it is answered once, here.

The project deliberately carries no `psutil` dependency (README, "Running it"),
and the standard library cannot read another process's command line on Windows.
This module therefore asks PowerShell -- always present on the target machine --
and parses its JSON reply.

A process counts as PetMatch's only when it runs a known entry point *and* was
launched from the project directory or the project's virtual environment. That
second condition is not pedantry: this machine also runs unrelated `run.py`
scripts belonging to other projects, and a shutdown helper that killed those
would be worse than no helper at all.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# These scripts are run with the project's interpreter, so the running
# executable identifies the virtual environment without hardcoding a
# user-specific path.
VIRTUALENV_SCRIPTS_DIRECTORY = Path(sys.executable).resolve().parent

_SUBPROCESS_TIMEOUT_SECONDS = 30

# taskkill's exit code for "there is no process with that id": it exited on its
# own between discovery and termination, which is the outcome we wanted anyway.
_TASKKILL_NO_SUCH_PROCESS = 128


class ProcessRole(Enum):
    """The part a process plays in a running PetMatch."""

    WEB_APPLICATION = "web application"
    AGENT_WORKER = "agent worker"
    OLLAMA_SERVER = "ollama server"


# Substrings identifying each entry point on a command line. The worker appears
# as `-m agent_service` or `agent_service` depending on how it was launched.
_ENTRY_POINT_MARKERS: dict[ProcessRole, tuple[str, ...]] = {
    ProcessRole.WEB_APPLICATION: ("run.py",),
    ProcessRole.AGENT_WORKER: ("agent_service",),
}


@dataclass(frozen=True)
class RunningProcess:
    """One live process belonging to PetMatch.

    Attributes:
        process_id: The operating-system process identifier.
        role: Which part of PetMatch this process is.
        command_line: The full command line, for reporting to the operator.
    """

    process_id: int
    role: ProcessRole
    command_line: str


def _run_powershell(script: str) -> str:
    """Execute a PowerShell snippet and return its standard output.

    Args:
        script: The PowerShell to run. It must write JSON to standard output.

    Returns:
        Standard output, or an empty string if PowerShell failed or timed out.
        A failure here means "cannot enumerate processes", which both callers
        treat as "nothing found" rather than crashing a cleanup helper.
    """
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout


@dataclass(frozen=True)
class _ProcessTableRow:
    """One row of the Windows process table.

    Attributes:
        process_id: The operating-system process identifier.
        parent_process_id: The id of the process that launched it, or 0.
        command_line: The full command line, empty when it could not be read.
    """

    process_id: int
    parent_process_id: int
    command_line: str


def _read_entry(entry: dict[str, object]) -> _ProcessTableRow | None:
    """Convert one PowerShell result row into a process table row.

    Args:
        entry: A decoded JSON object holding `ProcessId`, `ParentProcessId`
            and `CommandLine`.

    Returns:
        The parsed row, or None when it carries no usable process id.
        `CommandLine` is null for processes this user may not inspect, which is
        reported as an empty string rather than dropping the row.
    """
    raw_process_id = entry.get("ProcessId")
    if not isinstance(raw_process_id, int) or raw_process_id <= 0:
        return None

    raw_parent_id = entry.get("ParentProcessId")
    raw_command_line = entry.get("CommandLine")
    return _ProcessTableRow(
        process_id=raw_process_id,
        parent_process_id=raw_parent_id if isinstance(raw_parent_id, int) else 0,
        command_line=raw_command_line if isinstance(raw_command_line, str) else "",
    )


def _query_process_table(name_filter: str) -> list[_ProcessTableRow]:
    """Read the process table for processes matching a WMI filter.

    Args:
        name_filter: A WMI `-Filter` expression selecting executables by name.

    Returns:
        One row per process. Empty when nothing matches or the query could not
        run.
    """
    script = (
        f'Get-CimInstance Win32_Process -Filter "{name_filter}" '
        "| Select-Object ProcessId, ParentProcessId, CommandLine "
        "| ConvertTo-Json -Depth 3"
    )
    output = _run_powershell(script).strip()
    if not output:
        return []

    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        return []

    # PowerShell 5.1 emits a bare object rather than a list for a single match.
    rows = [parsed] if isinstance(parsed, dict) else parsed
    if not isinstance(rows, list):
        return []

    read = (_read_entry(row) for row in rows if isinstance(row, dict))
    return [row for row in read if row is not None]


def _belongs_to_this_project(command_line: str) -> bool:
    """Decide whether a command line was launched from this checkout.

    Args:
        command_line: The full command line of a candidate process.

    Returns:
        True when the command line references the project directory or the
        project's virtual environment. Paths are compared case-insensitively
        with slashes normalised, because Windows reports the same directory
        with forward and back slashes interchangeably.
    """
    normalised = command_line.replace("/", "\\").lower()
    return any(
        str(location).replace("/", "\\").lower() in normalised
        for location in (PROJECT_ROOT, VIRTUALENV_SCRIPTS_DIRECTORY)
    )


def _classify(command_line: str) -> ProcessRole | None:
    """Identify which PetMatch entry point a command line represents.

    Args:
        command_line: The full command line of a candidate process.

    Returns:
        The matching role, or None when the command line is not a PetMatch
        entry point launched from this checkout.
    """
    if not _belongs_to_this_project(command_line):
        return None
    return next(
        (
            role
            for role, markers in _ENTRY_POINT_MARKERS.items()
            if any(marker in command_line for marker in markers)
        ),
        None,
    )


def _claim_descendants(
    rows: list[_ProcessTableRow],
    roles_by_process_id: dict[int, ProcessRole],
) -> None:
    """Assign the role of a matched process to its Python descendants, in place.

    Launching `run.py` produces two processes on this machine: the interpreter
    named on the command line, and a child re-executed from the base Python
    installation. The child's command line carries neither the project
    directory nor the virtual environment, so path matching alone never finds
    it -- and a child left running keeps holding the port its parent released.
    Parentage identifies it precisely, where a looser path rule would start
    claiming other projects' processes.

    Args:
        rows: Every Python process on the machine.
        roles_by_process_id: Roles found by path matching. Extended in place
            with each descendant discovered.
    """
    # A child may be listed before its parent, so sweep until nothing new
    # appears rather than assuming the table is ordered.
    found_more = True
    while found_more:
        found_more = False
        for row in rows:
            if row.process_id in roles_by_process_id:
                continue
            inherited_role = roles_by_process_id.get(row.parent_process_id)
            if inherited_role is not None:
                roles_by_process_id[row.process_id] = inherited_role
                found_more = True


def find_petmatch_processes() -> list[RunningProcess]:
    """List the web and agent processes currently serving this checkout.

    A process qualifies either because its command line names a PetMatch entry
    point inside this checkout, or because it descends from one that does.

    Excludes the calling process, so a script may safely ask what is running
    without discovering -- and then killing -- itself.

    Returns:
        Every matching process, ordered by process id.
    """
    this_process_id = os.getpid()
    rows = [
        row
        for row in _query_process_table("Name='python.exe' or Name='pythonw.exe'")
        if row.process_id != this_process_id
    ]

    roles_by_process_id: dict[int, ProcessRole] = {}
    for row in rows:
        role = _classify(row.command_line)
        if role is not None:
            roles_by_process_id[row.process_id] = role

    _claim_descendants(rows, roles_by_process_id)

    found = [
        RunningProcess(row.process_id, roles_by_process_id[row.process_id], row.command_line)
        for row in rows
        if row.process_id in roles_by_process_id
    ]
    return sorted(found, key=lambda process: process.process_id)


def find_ollama_processes() -> list[RunningProcess]:
    """List the Ollama processes running on this machine.

    Ollama is shared machine-wide rather than owned by this checkout, so it is
    reported separately and never stopped unless explicitly requested.

    Returns:
        Every live Ollama process, ordered by process id.
    """
    processes = [
        RunningProcess(
            process_id=row.process_id,
            role=ProcessRole.OLLAMA_SERVER,
            command_line=row.command_line or "ollama",
        )
        for row in _query_process_table("Name='ollama.exe' or Name='ollama app.exe'")
    ]
    return sorted(processes, key=lambda process: process.process_id)


def terminate(process: RunningProcess) -> bool:
    """Forcibly stop one process.

    A development server holds an open socket and a database session. Asking it
    to exit gracefully from outside is not possible on Windows without a console
    control event, so it is terminated.

    Args:
        process: The process to stop.

    Returns:
        True when the process is no longer running afterwards, including when it
        had already exited between discovery and termination.
    """
    try:
        completed = subprocess.run(
            ["taskkill", "/PID", str(process.process_id), "/F"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode in (0, _TASKKILL_NO_SUCH_PROCESS)
