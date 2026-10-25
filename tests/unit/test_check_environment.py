"""Tests for the import bootstrap in `scripts/check_environment.py`.

Launching that file directly puts `scripts/` on `sys.path`, not the project
root. The database check needs `app.config` for the FreeTDS charset, so the
script has to add the root itself. These tests reproduce that launch path.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _run_as_a_script_under_scripts(probe: str) -> subprocess.CompletedProcess[str]:
    """Run `probe` with only `scripts/` prepended, the project root removed.

    That is the `sys.path` Python builds for `python scripts/check_environment.py`.
    """
    launcher = textwrap.dedent(
        """
        import sys
        from pathlib import Path

        root = Path(sys.argv[1]).resolve()
        sys.path = [
            entry
            for entry in sys.path
            if entry and Path(entry).resolve() != root
        ]
        sys.path.insert(0, str(root / "scripts"))
        """
    )
    return subprocess.run(
        [sys.executable, "-c", launcher + probe, str(PROJECT_ROOT)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_application_package_is_unreachable_from_the_scripts_directory_alone() -> None:
    """Proves a file launched under `scripts/` cannot import `app` by itself.

    This is the failure the environment script has to compensate for. If the
    package were already importable, a missing path insert would not show up.
    """
    probe = textwrap.dedent(
        """
        try:
            import app.config
        except ModuleNotFoundError:
            raise SystemExit(0)
        raise SystemExit("app was importable without the project root on sys.path")
        """
    )

    completed = _run_as_a_script_under_scripts(probe)

    assert completed.returncode == 0, completed.stderr


def test_environment_script_makes_the_application_package_importable() -> None:
    """Proves loading the environment script inserts the project root.

    Without that insert, `from app.config import SQL_SERVER_CLIENT_CHARSET`
    raises ModuleNotFoundError, and the database check reports it as a
    connectivity failure.
    """
    probe = textwrap.dedent(
        """
        import importlib.util
        from pathlib import Path

        root = Path(sys.argv[1]).resolve()
        spec = importlib.util.spec_from_file_location(
            "check_environment_as_script",
            root / "scripts" / "check_environment.py",
        )
        if spec is None or spec.loader is None:
            raise SystemExit("could not load check_environment.py")
        module = importlib.util.module_from_spec(spec)
        # dataclasses look the class's module up in sys.modules during decoration.
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        from app.config import SQL_SERVER_CLIENT_CHARSET

        if SQL_SERVER_CLIENT_CHARSET != "CP1252":
            raise SystemExit(f"unexpected charset {SQL_SERVER_CLIENT_CHARSET}")
        """
    )

    completed = _run_as_a_script_under_scripts(probe)

    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_web_search_check_reports_a_failure_without_the_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves the Tavily MCP check fails cleanly and never prints the key.

    The search itself is replaced, so no network is touched; the point is
    that the failure text shown on the console has the key scrubbed out.
    """
    from agent_service.tools import tavily_mcp
    from scripts import check_environment

    fake_key = "tvly-dev-NOT-A-REAL-KEY"

    def failing_search(api_key: str, query: str, max_results: int) -> list[object]:
        raise ConnectionError(f"https://mcp.tavily.com/mcp/?tavilyApiKey={api_key} refused")

    monkeypatch.setenv("TAVILY_API_KEY", fake_key)
    monkeypatch.setattr(tavily_mcp, "search_tavily_over_mcp", failing_search)

    result = check_environment.check_web_search()

    assert result.passed is False
    assert fake_key not in result.detail


def test_web_search_check_fails_when_no_key_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves a missing TAVILY_API_KEY is reported rather than attempted."""
    from scripts import check_environment

    monkeypatch.setenv("TAVILY_API_KEY", "")

    result = check_environment.check_web_search()

    assert result.passed is False
    assert "TAVILY_API_KEY" in result.detail
