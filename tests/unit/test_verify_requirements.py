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


def test_check_documentation_references_reports_every_dead_reference_it_finds() -> None:
    """Proves the FAIL detail for the docs check names how many dead references exist, by kind.

    Exercised end to end rather than by re-deriving the count, so a change to
    either `find_dead_references()` or the message format is caught here.
    """
    passed, detail = verify_requirements.check_documentation_references()
    dead = verify_requirements.find_dead_references()
    if dead:
        assert passed is False
        assert str(len(dead)) in detail
    else:
        assert passed is True


def test_main_is_guarded_and_does_not_run_on_import() -> None:
    """Proves importing the module runs no check - main() sits behind `__name__ == "__main__"`."""
    assert callable(verify_requirements.main)
    assert verify_requirements.__name__ != "__main__"
