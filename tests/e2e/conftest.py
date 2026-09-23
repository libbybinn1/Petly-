"""Fixtures for the end-to-end suite.

These drive a real browser against a real server, against the real cloud
database. That is deliberate: E2E is the only layer that proves the pieces
work *together*, and substituting a different database here would leave the
production dialect untested end to end.

The server is started once per session as a subprocess, because the point is
to exercise it the way a browser does rather than through Flask's test
client.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page

PROJECT_ROOT = Path(__file__).resolve().parents[2]

STAFF_EMAIL = "dana@petmatch.org"
ADOPTER_EMAIL = "maya@example.com"
DEMO_PASSWORD = "Password123!"

SERVER_START_TIMEOUT_SECONDS = 60


def _find_free_port() -> int:
    """Pick a port nothing is listening on.

    A fixed port would collide with a development server the developer
    already has running, producing a confusing suite that tests the wrong
    application.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_until_serving(port: int, process: subprocess.Popen) -> None:
    """Block until the server answers, or fail with its output.

    Raises:
        RuntimeError: The server exited or never became reachable.
    """
    deadline = time.monotonic() + SERVER_START_TIMEOUT_SECONDS

    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = (process.stdout.read() if process.stdout else "") or ""
            raise RuntimeError(f"server exited during startup:\n{output[-2000:]}")

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.5)

    raise RuntimeError(f"server did not start within {SERVER_START_TIMEOUT_SECONDS}s")


@pytest.fixture(scope="session")
def live_server() -> Iterator[str]:
    """Start the application on a free port for the whole session."""
    port = _find_free_port()

    process = subprocess.Popen(  # - fixed command, no user input
        [sys.executable, "run.py"],
        cwd=str(PROJECT_ROOT),
        env={"FLASK_PORT": str(port), "PATH": ""} | dict(__import__("os").environ) | {
            "FLASK_PORT": str(port)
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        _wait_until_serving(port, process)
        yield f"http://127.0.0.1:{port}"
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


# Pages query a shared free-tier cloud database over a corporate network, so
# a load of several seconds is normal here and is not a symptom.
PAGE_TIMEOUT_MS = 60_000


@pytest.fixture
def page(browser: Browser, live_server: str) -> Iterator[Page]:
    """A fresh browser page with console errors turned into test failures.

    A page that renders but throws in the console is broken; letting that
    pass silently would make the suite agree with a user who says "it looks
    fine" while the interface quietly misbehaves.
    """
    context = browser.new_context(viewport={"width": 1400, "height": 1000})
    context.set_default_timeout(PAGE_TIMEOUT_MS)
    context.set_default_navigation_timeout(PAGE_TIMEOUT_MS)
    new_page = context.new_page()

    console_errors: list[str] = []
    new_page.on(
        "console",
        lambda message: console_errors.append(message.text)
        if message.type == "error"
        else None,
    )
    new_page.on("pageerror", lambda error: console_errors.append(str(error)))

    yield new_page

    context.close()
    assert not console_errors, f"browser reported errors: {console_errors}"


def sign_in(page: Page, base_url: str, email: str) -> None:
    """Sign in through the real form."""
    page.goto(f"{base_url}/login", wait_until="load")
    page.fill("#email", email)
    page.fill("#password", DEMO_PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_load_state("load")
