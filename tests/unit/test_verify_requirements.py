"""Tests for the requirement-verification script itself.

`scripts/verify_requirements.py` is imported here as an ordinary module -
`main()` is guarded by `if __name__ == "__main__":`, so importing it runs no
check and prints nothing. These tests are about the checker's own
reliability, not about whether the project currently passes every check: a
`FAIL` from a check that ran cleanly is the tool doing its job, so the one
property every check must have is that it *returns* a verdict rather than
raising an exception that would take the whole CLI run down with it.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from scripts import verify_requirements

pytestmark = pytest.mark.unit


def test_every_check_returns_a_bool_and_a_string_without_raising() -> None:
    """Proves each entry in ALL_CHECKS yields a `(bool, str)` verdict, never an exception.

    A check is allowed to report FAIL - that is how it names a real gap. It
    is never allowed to crash the run: `main()` only catches exceptions as a
    last resort, so this is what actually guarantees one broken check cannot
    stop every other requirement from being reported.
    """
    for check in verify_requirements.ALL_CHECKS:
        passed, detail = check.verify()
        assert isinstance(passed, bool), f"{check.requirement} returned {type(passed)}"
        assert isinstance(detail, str), f"{check.requirement} returned {type(detail)}"
        assert detail, f"{check.requirement} returned an empty detail"


def test_check_requirement_labels_are_unique() -> None:
    """Proves ALL_CHECKS has no duplicate label, which would silently shadow one check's result."""
    labels = [check.requirement for check in verify_requirements.ALL_CHECKS]
    assert len(labels) == len(set(labels))


def test_forms_counter_finds_at_least_the_seven_spec_21_forms() -> None:
    """Proves post_form_endpoints() counts at least the seven spec 21 business forms today.

    Blueprint 4.5 and spec 21 require seven meaningful business forms. The
    project currently ships more (each spec 21 form plus a few extra POST
    targets such as invitation dispatch), so this asserts the floor rather
    than an exact count that would need editing every time a form is added.
    """
    posted = verify_requirements.post_form_endpoints()
    assert len(posted) >= verify_requirements.MINIMUM_BUSINESS_FORMS, sorted(posted)


def test_doc_reference_scanner_flags_a_fabricated_reference() -> None:
    """Proves the scanner used by check_documentation_references() flags a made-up test name."""
    kind = verify_requirements._classify_reference(
        "test_a_function_invented_for_this_assertion",
        tests=set(),
        classes=set(),
    )
    assert kind == "test"


def test_the_dead_reference_scanner_always_answers_with_a_list() -> None:
    """Proves the scanner returns a verdict either way, rather than only when it fails.

    The assertions here used to sit inside `if dead:`, so on a clean run this
    test asserted nothing at all - and a scanner that had started raising, or
    returning None, would have passed it.
    """
    dead = verify_requirements.find_dead_references()

    assert isinstance(dead, list)
    for reference in dead:
        assert reference.document
        assert reference.kind in ("test", "path", "identifier")


def test_check_documentation_references_agrees_with_its_own_scanner() -> None:
    """Proves the verdict and the detail follow from what the scanner found.

    Exercised end to end rather than by re-deriving the count, so a change to
    either `find_dead_references()` or the message format is caught here.
    """
    passed, detail = verify_requirements.check_documentation_references()
    dead = verify_requirements.find_dead_references()

    assert passed is (dead == [])
    assert detail
    if dead:
        # The failing detail has to name the size of the problem; the passing
        # one says what it checked instead, and has no count to carry.
        assert str(len(dead)) in detail


def test_importing_the_module_runs_no_check() -> None:
    """Proves `main()` sits behind a `__name__ == "__main__"` guard.

    Asserted by parsing the module rather than by comparing `__name__` to
    "__main__" - which is true of every imported module ever written, so the
    previous version of this test could not fail. What matters is that the
    module body contains no top-level call: importing it must not run the
    checks, because the test suite imports it.
    """
    source = pathlib.Path(verify_requirements.__file__).read_text(encoding="utf-8")
    module = ast.parse(source)

    top_level_calls = [
        node
        for node in module.body
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
    ]

    assert callable(verify_requirements.main)
    assert top_level_calls == [], [ast.unparse(node) for node in top_level_calls]


def test_the_main_guard_rule_would_catch_a_module_that_runs_on_import() -> None:
    """Proves the check above tests something, using a module that breaks it.

    Negative half of the pair: the rule is spelled out over a synthetic
    source so it cannot pass merely because the real file is well behaved.
    """
    module = ast.parse("def main() -> int:\n    return 0\n\n\nmain()\n")

    top_level_calls = [
        node
        for node in module.body
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
    ]

    assert len(top_level_calls) == 1
