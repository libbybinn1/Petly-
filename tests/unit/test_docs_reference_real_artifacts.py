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

This suite is expected to fail today. The dead-reference list it reports is
exactly what BG-3 of the requirements audit described: `docs/TESTING.md` and
`docs/FEATURES.md` cite dozens of test files that were renamed or never
written, and `PLANNING.md` cites paths from an earlier layout. Fixing them is
a documentation task for the docs engineer, not a code change - which is why
this file is a pure reader of `docs/`, `PLANNING.md`, `README.md` and
CLAUDE.md and never edits any of them.
"""

from __future__ import annotations

import pytest
from scripts.verify_requirements import _classify_reference, find_dead_references

pytestmark = pytest.mark.unit


@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN GAP, being fixed by the documentation sweep: docs/TESTING.md, PLANNING.md and "
        "docs/ARCHITECTURE.md still cite tests and paths that do not exist. Remove this marker "
        "in the same change that fixes the references; strict=True makes the fix flip it red "
        "until the marker goes."
    ),
)
def test_documents_reference_no_dead_test_path_or_identifier() -> None:
    """Proves every backticked test, path and class name in the docs resolves.

    This is the one test in the new suites that is allowed to fail today: the
    dead references it reports are real, pre-existing documentation drift
    (CLAUDE.md R5), not a defect in the scanner. Run this file directly to
    see the full sorted list; each line is `file:line -> missing <kind>
    `<reference>``, precise enough to open and fix.
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
