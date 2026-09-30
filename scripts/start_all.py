"""Start everything PetMatch needs, each part in its own console window.

A working demo needs three processes: Ollama serving the local models, the
Flask web application, and the independent agent worker that drains the
`analysis_jobs` queue (CLAUDE.md R2 -- the agent is a separate OS process and
never an import of the Flask app). Starting them by hand means three terminals
and three commands in the right order; this script does it in one.

Each process gets its own console window so its log stays readable and so
closing one does not take the others with it. Stop them all again with
`scripts/stop_all.py`.

Ollama no longer starts itself at login: its shortcut was removed from the
Windows Startup folder so that CPU-only inference never competes for this
laptop's memory unprompted. This script starts it on demand instead, which is
why it is the right way to bring PetMatch up.

Usage:
    C:/Users/libbyb/venvs/petmatch/Scripts/python.exe scripts/start_all.py
    ... scripts/start_all.py --restart      # stop anything already running first
    ... scripts/start_all.py --no-ollama    # leave Ollama alone
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.process_control import (  # noqa: E402
    find_ollama_processes,
    find_petmatch_processes,
    terminate,
)

# Ollama installs per-user and is not always on PATH for a non-login shell.
_OLLAMA_FALLBACK_PATH = Path(
    os.environ.get("LOCALAPPDATA", "")
) / "Programs" / "Ollama" / "ollama.exe"

# Ollama needs a moment to bind its port before the agent tries to reach it.
_OLLAMA_WARMUP_SECONDS = 3

_DEFAULT_FLASK_PORT = "5000"


def _parse_arguments() -> argparse.Namespace:
    """Read the command-line options.

    Returns:
        The parsed options, with `restart` and `no_ollama` flags.
    """
    parser = argparse.ArgumentParser(
        description="Start Ollama, the PetMatch web application and the agent worker.",
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="Stop any PetMatch process already running before starting.",
    )
    parser.add_argument(
        "--no-ollama",
        action="store_true",
        help="Do not start Ollama. Use when it is already running or not needed.",
    )
    return parser.parse_args()


def _launch_in_new_console(command: list[str], working_directory: Path) -> None:
    """Start a command in its own console window and return immediately.

    Args:
        command: The executable and its arguments.
        working_directory: The directory to start the process in.
    """
    subprocess.Popen(
        command,
        cwd=str(working_directory),
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )


def _find_ollama_executable() -> Path | None:
    """Locate the Ollama executable.

    Returns:
        The path to `ollama.exe`, or None when it is not installed where this
        machine puts it and is not on PATH.
    """
    on_path = shutil.which("ollama")
    if on_path:
        return Path(on_path)
    if _OLLAMA_FALLBACK_PATH.is_file():
        return _OLLAMA_FALLBACK_PATH
    return None


def _start_ollama() -> bool:
    """Start the Ollama server unless it is already running.

    Returns:
        True when Ollama is running by the time this returns.
    """
    if find_ollama_processes():
        print("  Ollama            already running")
        return True

    executable = _find_ollama_executable()
    if executable is None:
        print("  Ollama            NOT FOUND - start it yourself, or pass --no-ollama")
        return False

    _launch_in_new_console([str(executable), "serve"], PROJECT_ROOT)
    time.sleep(_OLLAMA_WARMUP_SECONDS)
    print(f"  Ollama            started ({executable})")
    return True


def _stop_running_processes() -> None:
    """Terminate every PetMatch process currently running."""
    running = find_petmatch_processes()
    if not running:
        return
    print(f"Stopping {len(running)} process(es) already running:")
    for process in running:
        outcome = "stopped" if terminate(process) else "COULD NOT STOP"
        print(f"  [{process.process_id:>6}] {process.role.value:<16} {outcome}")
    print()


def _report_already_running() -> None:
    """Explain that PetMatch is up and how to replace it."""
    print("PetMatch is already running:")
    for process in find_petmatch_processes():
        print(f"  [{process.process_id:>6}] {process.role.value}")
    print("\nUse --restart to replace it, or scripts/stop_all.py to shut it down.")


def main() -> int:
    """Bring up Ollama, the web application and the agent worker.

    Returns:
        0 once every process has been launched, 1 when PetMatch was already
        running and `--restart` was not given.
    """
    options = _parse_arguments()

    if options.restart:
        _stop_running_processes()
    elif find_petmatch_processes():
        _report_already_running()
        return 1

    print("Starting PetMatch:")
    if not options.no_ollama:
        _start_ollama()

    interpreter = sys.executable
    _launch_in_new_console([interpreter, "run.py"], PROJECT_ROOT)
    print("  Web application   started")

    _launch_in_new_console([interpreter, "-m", "agent_service"], PROJECT_ROOT)
    print("  Agent worker      started")

    port = os.environ.get("FLASK_PORT", _DEFAULT_FLASK_PORT)
    print(f"\nPetMatch is coming up at http://127.0.0.1:{port}")
    print("Each process has its own console window. Stop them with:")
    print("    python scripts/stop_all.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
