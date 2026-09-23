"""Structural checks on the CSRF tokens in every template (NFR-4.3).

These exist because of a real defect. The tokens were inserted by a
line-oriented script, and one `<form` tag spans several lines - so the
hidden input landed between `<form method="post"` and its `action`
attribute. The browser recovers by rendering the action as visible text,
which left that form with no action and no token: it posted to the current
URL and was refused.

Nothing caught it. The E2E suite signs in and browses but never withdraws
an application, and no unit test reads a template. A check that parses
every form is the one that would have, and it costs nothing to run.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

TEMPLATE_DIRECTORY = Path(__file__).resolve().parents[2] / "app" / "templates"
TOKEN_FIELD = "csrf_token"


def _form_tags(markup: str) -> list[tuple[str, str]]:
    """Return every form's opening tag and body.

    Returns:
        One (opening tag, body) pair per `<form>` in the markup.
    """
    forms: list[tuple[str, str]] = []
    for match in re.finditer(r"<form\b", markup):
        tag_end = markup.index(">", match.start())
        opening_tag = markup[match.start() : tag_end + 1]
        closing = markup.find("</form>", tag_end)
        body = markup[tag_end + 1 : closing if closing != -1 else len(markup)]
        forms.append((opening_tag, body))
    return forms


def _templates() -> list[Path]:
    """Every template in the application."""
    return sorted(TEMPLATE_DIRECTORY.rglob("*.html"))


class TestEveryFormIsWellFormed:
    """A token in the wrong place is worse than no token: it breaks the form."""

    def test_there_are_templates_to_check(self) -> None:
        """Proves the search found something, so a pass means something."""
        assert len(_templates()) > 5

    def test_no_token_sits_inside_an_opening_form_tag(self) -> None:
        """Proves the exact defect that broke the Withdraw button cannot recur.

        An input inside the opening tag swallows the attributes after it,
        so the form loses both its action and its token at once.
        """
        misplaced = [
            path.name
            for path in _templates()
            for opening_tag, _ in _form_tags(path.read_text(encoding="utf-8"))
            if TOKEN_FIELD in opening_tag
        ]

        assert misplaced == [], f"token inside a <form ...> tag in {misplaced}"

    def test_every_post_form_carries_a_token(self) -> None:
        """Proves no form was missed when CSRF protection went in.

        A missed token does not fail quietly: CSRFProtect rejects the post
        with a 400, so the feature simply stops working.
        """
        untokened = [
            path.name
            for path in _templates()
            for opening_tag, body in _form_tags(path.read_text(encoding="utf-8"))
            if 'method="post"' in opening_tag.lower() and TOKEN_FIELD not in body
        ]

        assert untokened == [], f"POST form with no CSRF token in {untokened}"

    def test_get_forms_do_not_carry_a_token(self) -> None:
        """Proves search forms were left alone.

        A token on a GET form puts it in the query string of every search,
        where it ends up in history and in shared links for no benefit -
        CSRFProtect does not check GET requests.
        """
        tokened_get_forms = [
            path.name
            for path in _templates()
            for opening_tag, body in _form_tags(path.read_text(encoding="utf-8"))
            if 'method="post"' not in opening_tag.lower() and TOKEN_FIELD in body
        ]

        assert tokened_get_forms == [], f"GET form with a token in {tokened_get_forms}"

    def test_every_form_is_closed(self) -> None:
        """Proves no template has an unbalanced form tag.

        An unclosed form silently swallows the markup after it, which is
        how the token above ended up somewhere it could do harm.
        """
        unbalanced = [
            path.name
            for path in _templates()
            if path.read_text(encoding="utf-8").count("<form")
            != path.read_text(encoding="utf-8").count("</form>")
        ]

        assert unbalanced == [], f"unbalanced <form> tags in {unbalanced}"
