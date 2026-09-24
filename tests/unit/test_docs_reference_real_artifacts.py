"""Proves docs/ and code never contradict each other - CLAUDE.md R5.

CLAUDE.md R5 says code and `docs/` may never contradict, and blueprint 18
makes "no contradiction between the documents and the code" a Definition-of-
Done item. This suite mechanically resolves three classes of claim every
markdown document makes in backticks:

* a name shaped like `test_something` must be a real, collectible test
  function (found by parsing every `tests/**/test_*.py` with `ast`, never by
  running pytest, so a suite that needs a browser or a live model is still
  checked);
* a path shaped like `app/...`, `tests/...`, `docs/...` and so on must exist
  on disk;
* an identifier shaped like `SomethingCommand`/`Query`/`Handler`/`Error` must
  be a class defined somewhere under `app/` or `agent_service/`.

The scanner itself lives in `scripts/verify_requirements.py` and is imported
from here, not reimplemented, so the CLI's dead-reference count and this
suite's failures are always about the exact same references.

This suite was an expected failure when it was written. It reported the 48
dead references BG-3 of the requirements audit described: `docs/TESTING.md`
and `docs/FEATURES.md` cited dozens of test files that had been renamed or
never written, and `PLANNING.md` cited paths from an earlier layout. The
documentation sweep of 2026-09-24 fixed all of them and removed the marker,
so the suite now guards the rule rather than recording a breach of it.

Fixing a failure here is a documentation task, not a code change - which is
why this file is a pure reader of `docs/`, `PLANNING.md`, `README.md` and
CLAUDE.md and never edits any of them.
"""

from __future__ import annotations

import pytest
from scripts.verify_requirements import _classify_reference, find_dead_references

pytestmark = pytest.mark.unit


def test_documents_reference_no_dead_test_path_or_identifier() -> None:
    """Proves every backticked test, path and class name in the docs resolves.

    This test used to be an expected failure: the requirements audit found 48
    dead references across docs/TESTING.md, PLANNING.md and
    docs/ARCHITECTURE.md - tests that were renamed or never written, and
    paths from an earlier layout. The documentation sweep of 2026-09-24 fixed
    every one, and the marker went with them, so this now guards CLAUDE.md R5
    rather than recording a breach of it.

    On failure the message is the full sorted list; each line is
    `file:line -> missing <kind>`, precise enough to open and fix.
    """
    dead = find_dead_references()
    assert dead == [], "\n" + "\n".join(str(reference) for reference in sorted(dead))


def test_scanner_flags_a_fabricated_test_name() -> None:
    """Proves the scanner reports a `test_*`-shaped name that nothing collects."""
    kind = _classify_reference(
        "test_this_function_was_never_written",
        tests=set(),
        classes=set(),
    )
    assert kind == "test"


def test_scanner_accepts_a_test_name_that_was_actually_collected() -> None:
    """Proves a genuinely collected test name is not reported as dead."""
    kind = _classify_reference(
        "test_this_function_was_never_written",
        tests={"test_this_function_was_never_written"},
        classes=set(),
    )
    assert kind is None


def test_scanner_flags_a_fabricated_project_path() -> None:
    """Proves the scanner reports a project-shaped path that does not exist on disk."""
    kind = _classify_reference(
        "tests/integration/test_a_file_that_was_never_created.py",
        tests=set(),
        classes=set(),
    )
    assert kind == "path"


def test_scanner_accepts_a_path_that_really_exists() -> None:
    """Proves a real path (this very file) is not reported as dead."""
    kind = _classify_reference(
        "tests/unit/test_docs_reference_real_artifacts.py",
        tests=set(),
        classes=set(),
    )
    assert kind is None


def test_scanner_flags_a_fabricated_cqrs_class_name() -> None:
    """Proves a `...Command`-shaped name with no matching class is flagged."""
    kind = _classify_reference("ThisCommandDoesNotExistCommand", tests=set(), classes=set())
    assert kind == "identifier"


def test_scanner_accepts_a_class_that_was_actually_defined() -> None:
    """Proves a class name present in the supplied set is not reported as dead."""
    kind = _classify_reference(
        "ApproveApplicationCommand",
        tests=set(),
        classes={"ApproveApplicationCommand"},
    )
    assert kind is None


def test_scanner_leaves_ordinary_prose_alone() -> None:
    """Proves backticked text that matches none of the three shapes is never flagged."""
    kind = _classify_reference("HTTP 404", tests=set(), classes=set())
    assert kind is None
