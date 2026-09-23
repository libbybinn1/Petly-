"""Fixtures for the end-to-end suite.

These drive a real browser against a real server, started as a subprocess so
the application is exercised the way a browser exercises it rather than
through Flask's test client.

**Server output goes to a file, never to a pipe.** Flask logs a line per
request, and an unread `subprocess.PIPE` fills its OS buffer after a few
dozen requests - at which point the server blocks forever on write and
silently stops answering. The symptom is navigation timeouts that look
exactly like slow pages, which makes it an unpleasant bug to chase.

**The database is a seeded local SQLite file, not the cloud one.** That was
not the first choice: the suite originally ran against Somee, on the
reasoning that E2E should exercise the production dialect. But Somee's free
tier throttles under the request fan-out of a browser page load, and an
end-to-end suite should test user journeys rather than a hosting tier's rate
limits.

What that trades away is worth stating plainly: these tests do not exercise
the SQL Server dialect. That is covered elsewhere - the integration suite
runs the event store and the approval cascade against the real schema,
`scripts/check_environment.py` verifies the live connection, and the running
application uses Somee for everything.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page

from tests.e2e.seed_e2e import (
    ADOPTER_EMAIL,
    DEMO_PASSWORD,
    STAFF_EMAIL,
    seed_e2e_database,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

SERVER_START_TIMEOUT_SECONDS = 60

# A ceiling, not a target. A cold first request builds the engine and opens
# a connection, which takes a moment; steady-state pages are far quicker.
PAGE_TIMEOUT_MS = 30_000

__all__ = ["ADOPTER_EMAIL", "DEMO_PASSWORD", "STAFF_EMAIL", "sign_in"]


def _find_free_port() -> int:
    """Pick a port nothing is listening on.

    A fixed port would collide with a development server the developer
    already has running, and the suite would silently test that instead.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_until_serving(
    port: int, process: subprocess.Popen[str], log_path: Path
) -> None:
    """Block until the server answers a real request.

    Checks HTTP rather than only TCP: a listening socket proves a process is
    bound to the port, not that it can serve a page.

    Raises:
        RuntimeError: The server exited, or never answered in time.
    """
    deadline = time.monotonic() + SERVER_START_TIMEOUT_SECONDS

    while time.monotonic() < deadline:
        if process.poll() is not None:
            log = log_path.read_text(encoding="utf-8", errors="replace")
            raise RuntimeError(f"server exited during startup:\n{log[-2000:]}")

        try:
            with urllib.request.urlopen(  # - fixed localhost URL
                f"http://127.0.0.1:{port}/login", timeout=10
            ) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            time.sleep(0.5)

    log = log_path.read_text(encoding="utf-8", errors="replace")
    raise RuntimeError(
        f"server never answered within {SERVER_START_TIMEOUT_SECONDS}s:\n{log[-2000:]}"
    )


@pytest.fixture(scope="session")
def e2e_database(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Create and seed the database the E2E server will use."""
    database_path = tmp_path_factory.mktemp("e2e_db") / "petmatch_e2e.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    seed_e2e_database(database_url)
    return database_url


@pytest.fixture(scope="session")
def live_server(
    tmp_path_factory: pytest.TempPathFactory, e2e_database: str
) -> Iterator[str]:
    """Start the application on a free port for the whole session."""
    port = _find_free_port()
    log_path = tmp_path_factory.mktemp("server") / "server.log"

    with log_path.open("w", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            [sys.executable, "run.py"],
            cwd=str(PROJECT_ROOT),
            env={
                **os.environ,
                "FLASK_PORT": str(port),
                "LOCAL_DATABASE_URL": e2e_database,
            },
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )

        try:
            _wait_until_serving(port, process, log_path)
            yield f"http://127.0.0.1:{port}"
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


# Chromium reports every non-2xx response as a console error. Tests that
# assert on a 403 or a 404 would otherwise fail for succeeding.
HTTP_STATUS_NOISE = "Failed to load resource: the server responded with a status of"


def _is_expected_http_status(message_text: str) -> bool:
    """Whether a console error is just Chromium reporting a non-2xx status."""
    return HTTP_STATUS_NOISE in message_text


@pytest.fixture
def page(browser: Browser, live_server: str) -> Iterator[Page]:
    """A fresh browser page with console errors turned into test failures.

    A page that renders but throws in the console is broken; letting that
    pass would make the suite agree with a user who says "it looks fine"
    while the interface quietly misbehaves.
    """
    context = browser.new_context(viewport={"width": 1400, "height": 1000})
    context.set_default_timeout(PAGE_TIMEOUT_MS)
    context.set_default_navigation_timeout(PAGE_TIMEOUT_MS)
    new_page = context.new_page()

    console_errors: list[str] = []

    def record_console_error(message: object) -> None:
        """Record genuine console errors, ignoring expected HTTP statuses.

        Chromium logs "Failed to load resource" for any non-2xx response,
        including the 400s, 403s and 404s several tests provoke on purpose.
        Treating those as failures would make a test fail *because* it
        proved the server refused something correctly.
        """
        if message.type != "error":  # type: ignore[attr-defined]
            return
        if _is_expected_http_status(message.text):  # type: ignore[attr-defined]
            return
        console_errors.append(message.text)  # type: ignore[attr-defined]

    new_page.on("console", record_console_error)
    new_page.on("pageerror", lambda error: console_errors.append(str(error)))

    yield new_page

    context.close()
    assert not console_errors, f"browser reported errors: {console_errors}"


def sign_in(page: Page, base_url: str, email: str) -> None:
    """Sign in through the real form.

    Waits for the redirect away from `/login` rather than for a load state,
    because a load state describes whichever document is current and can
    answer about the login page itself.
    """
    page.goto(f"{base_url}/login", wait_until="load")
    page.fill("#email", email)
    page.fill("#password", DEMO_PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(lambda url: "/login" not in url)
