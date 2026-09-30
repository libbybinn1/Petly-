"""Shut down every PetMatch process left running on this machine.

The Flask development server and the agent worker are long-lived foreground
processes. Closing the terminal window that launched one does not always stop
it, and a stopped debug session can leave the server behind. They then keep
listening on their port, so the next launch either fails to bind or -- worse --
the browser reaches a week-old copy of the code and the demo shows stale
behaviour.

Run this before starting a session, after finishing one, and any time
`run.py` reports that its port is already in use.

Usage:
    C:/Users/libbyb/venvs/petmatch/Scripts/python.exe scripts/stop_all.py
    ... scripts/stop_all.py --dry-run          # list, change nothing
    ... scripts/stop_all.py --include-ollama   # also unload the model from RAM
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.process_control import (  # noqa: E402
    RunningProcess,
    find_ollama_processes,
    find_petmatch_processes,
    terminate,
)

# Keeps one reported process on one terminal line, ellipsis included.
_COMMAND_LINE_DISPLAY_WIDTH = 70


def _parse_arguments() -> argparse.Namespace:
    """Read the command-line options.

    Returns:
        The parsed options, with `dry_run` and `include_ollama` flags.
    """
    parser = argparse.ArgumentParser(
        description="Stop the PetMatch web application and agent worker.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be stopped without stopping anything.",
    )
    parser.add_argument(
        "--include-ollama",
        action="store_true",
        help="Also stop Ollama, freeing the several gigabytes a loaded model holds.",
    )
    return parser.parse_args()


def _describe(process: RunningProcess) -> str:
    """Render one process as a single readable line.

    Args:
        process: The process to describe.

    Returns:
        A line naming the role and process id, with the command line trimmed to
        stay readable in a terminal.
    """
    command_line = process.command_line.strip()
    if len(command_line) > _COMMAND_LINE_DISPLAY_WIDTH:
        command_line = f"{command_line[: _COMMAND_LINE_DISPLAY_WIDTH - 3]}..."
    return f"  [{process.process_id:>6}] {process.role.value:<16} {command_line}"


def _stop_all(processes: list[RunningProcess]) -> int:
    """Terminate every given process and report each outcome.

    Args:
        processes: The processes to stop.

    Returns:
        How many were confirmed stopped.
    """
    stopped_count = 0
    for process in processes:
        succeeded = terminate(process)
        outcome = "stopped" if succeeded else "COULD NOT STOP"
        print(f"  [{process.process_id:>6}] {process.role.value:<16} {outcome}")
        stopped_count += int(succeeded)
    return stopped_count


def main() -> int:
    """Find and stop the running PetMatch processes.

    Returns:
        0 when nothing was left running or everything stopped, 1 when at least
        one process survived termination and needs manual attention.
    """
    options = _parse_arguments()

    targets = find_petmatch_processes()
    if options.include_ollama:
        targets += find_ollama_processes()

    if not targets:
        print("Nothing to stop: no PetMatch process is running.")
        return 0

    print(f"Found {len(targets)} running process(es):")
    for process in targets:
        print(_describe(process))

    if options.dry_run:
        print("\nDry run: nothing was stopped.")
        return 0

    print("\nStopping:")
    stopped_count = _stop_all(targets)

    survived_count = len(targets) - stopped_count
    print(f"\nStopped {stopped_count} of {len(targets)}.")
    if survived_count:
        print(f"{survived_count} process(es) survived. Stop them from Task Manager.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
