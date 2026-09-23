"""Automated accessibility checks in a real browser (blueprint section 17).

docs/UX.md section 8 makes four promises: a visible focus ring, labelled
controls, status conveyed by text as well as colour, and colour pairs chosen
for contrast in both themes. Promises in a document are not evidence, and the
first audit of this interface found 251 contrast failures across fifteen
screens while section 8 still claimed they had been checked. These tests are
what stops that happening again.

The checker is axe-core, loaded from a CDN. Only `serious` and `critical`
violations fail a test: axe's `minor` and `moderate` findings include advice
this project has deliberately declined, and a suite that fails on advice gets
switched off.

Nothing here asserts on generated text (rule R3). Every assertion is about
document structure or a measured colour ratio, both of which are the same on
every run.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

AXE_URL = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js"

# Screens an anonymous visitor can reach, which is every screen whose markup
# the design system owns end to end.
ANONYMOUS_SCREENS = [
    ("/", "home"),
    ("/animals/", "browse"),
    ("/login", "sign in"),
    ("/animals/no-such-animal", "404"),
]

SERIOUS = {"serious", "critical"}


def _run_axe(page: Page) -> list[dict[str, object]]:
    """Inject axe-core and return its violations for the current document.

    Returns:
        One entry per violated rule, each with its id, impact and node count.

    Raises:
        RuntimeError: axe-core could not be loaded, so an empty result would
            be indistinguishable from a clean page.
    """
    try:
        page.add_script_tag(url=AXE_URL)
    except Exception as error:  # any load failure is fatal here
        raise RuntimeError(f"axe-core did not load from {AXE_URL}: {error}") from error

    return page.evaluate(
        """async () => {
             const report = await axe.run(document, {resultTypes: ['violations']});
             return report.violations.map(v => ({
               id: v.id, impact: v.impact, nodes: v.nodes.length,
               help: v.help,
               sample: v.nodes.slice(0, 3).map(n => n.html.slice(0, 200))
             }));
           }"""
    )


def _serious_violations(page: Page) -> list[dict[str, object]]:
    """The violations that fail a test: serious and critical only."""
    return [item for item in _run_axe(page) if item["impact"] in SERIOUS]


class TestAnonymousScreensPassAxe:
    """Every screen a visitor can reach without an account."""

    @pytest.mark.parametrize(("path", "name"), ANONYMOUS_SCREENS)
    def test_screen_has_no_serious_violations(
        self, page: Page, live_server: str, path: str, name: str
    ) -> None:
        """Proves the screen has no serious or critical axe violation.

        This is the regression guard for the contrast tokens, the focus ring,
        the skip link and the landmark structure all at once: each of those
        appears to axe as a rule it can measure, so a token edited back to an
        unreadable value fails here rather than in a later audit.
        """
        page.goto(f"{live_server}{path}", wait_until="load")
        violations = _serious_violations(page)
        assert not violations, f"{name} ({path}) has serious axe violations: {violations}"

    @pytest.mark.parametrize(("path", "name"), ANONYMOUS_SCREENS)
    def test_screen_passes_in_dark_mode_too(
        self, page: Page, live_server: str, path: str, name: str
    ) -> None:
        """Proves the dark theme is checked, not merely supported.

        Dark mode is a token override rather than a second stylesheet
        (docs/UX.md section 2), so a colour can be safe in one theme and fail
        in the other - which is exactly what happened to the primary button,
        at 3.74:1 light and 2.80:1 dark. Emulating the media query here means
        neither theme can regress unobserved.
        """
        page.emulate_media(color_scheme="dark")
        page.goto(f"{live_server}{path}", wait_until="load")
        violations = _serious_violations(page)
        assert not violations, f"{name} ({path}) fails in dark mode: {violations}"


class TestStructuralPromises:
    """The parts of docs/UX.md section 8 worth asserting on directly."""

    def test_every_page_offers_a_skip_link_to_the_main_landmark(
        self, page: Page, live_server: str
    ) -> None:
        """Proves the skip link is the first tab stop and targets <main>.

        A keyboard user should not walk eight navigation links on every
        screen. axe cannot check this - a skip link that points at nothing,
        or sits tenth in the tab order, passes every rule - so it is asserted
        here.
        """
        page.goto(f"{live_server}/animals/", wait_until="load")
        page.keyboard.press("Tab")

        focused_class = page.evaluate("() => document.activeElement.className")
        assert "skip-link" in focused_class, (
            f"the first tab stop is {focused_class!r}, not the skip link"
        )

        target = page.evaluate(
            "() => document.activeElement.getAttribute('href')"
        )
        assert target == "#main"
        assert page.locator("main#main").count() == 1, "the skip link must land on <main>"

    def test_the_current_page_is_marked_in_the_navigation(
        self, page: Page, live_server: str
    ) -> None:
        """Proves the active nav link carries aria-current, not only a colour.

        The bar highlights where you are with a tinted background. A screen
        reader cannot see a tint, so the same fact is also stated in the
        markup.
        """
        page.goto(f"{live_server}/animals/", wait_until="load")
        current = page.locator('.topbar__nav [aria-current="page"]')
        assert current.count() == 1
        assert "navlink--active" in (current.first.get_attribute("class") or "")

    def test_the_error_page_has_a_first_level_heading(
        self, page: Page, live_server: str
    ) -> None:
        """Proves a 404 is a real page, with an h1 and a way onward.

        The error screen used to open with an h3 and offer one link back to
        the home page - which is rarely where somebody who hit a dead end was
        trying to go.
        """
        page.goto(f"{live_server}/animals/no-such-animal", wait_until="load")
        assert page.locator("h1").count() == 1
        assert page.locator(".empty__actions .btn").count() >= 2

    def test_focus_is_visible_on_every_control_in_the_tab_order(
        self, page: Page, live_server: str
    ) -> None:
        """Proves no focusable control has its focus ring removed.

        docs/UX.md section 8 promised a visible ring and the stylesheet had no
        `:focus-visible` rule at all, so every link and card relied on the
        browser default - and `.input` removed even that with `outline: none`.
        """
        page.goto(f"{live_server}/animals/", wait_until="load")

        for _ in range(14):
            page.keyboard.press("Tab")
            ring = page.evaluate(
                """() => {
                     const style = getComputedStyle(document.activeElement);
                     return {tag: document.activeElement.tagName,
                             cls: String(document.activeElement.className),
                             style: style.outlineStyle,
                             width: parseFloat(style.outlineWidth)};
                   }"""
            )
            assert ring["style"] != "none" and ring["width"] >= 2, (
                f"{ring['tag']}.{ring['cls']} shows no focus ring: {ring}"
            )


class TestTheHarnessDetectsFailures:
    """A checker nobody has seen fail is a checker nobody should trust."""

    def test_an_injected_low_contrast_element_is_reported(
        self, page: Page, live_server: str
    ) -> None:
        """Proves the axe harness actually fails when contrast is wrong.

        Negative test (rule R3). Every assertion above is that axe found
        nothing, and a broken injection, a blocked CDN or a silently changed
        API would produce exactly that result. So this test puts a knowingly
        unreadable element on a page that passes, and requires the same
        harness to catch it.

        The colours are chosen to fail unambiguously: #b9b0ab on #c8c1bc is
        about 1.2:1, against a required 4.5:1.
        """
        page.goto(f"{live_server}/", wait_until="load")
        page.evaluate(
            """() => {
                 const bad = document.createElement('p');
                 bad.id = 'deliberately-unreadable';
                 bad.textContent = 'This sentence is not readable by design.';
                 bad.setAttribute('style',
                   'color:#b9b0ab;background:#c8c1bc;font-size:14px;padding:8px');
                 document.querySelector('main').appendChild(bad);
               }"""
        )

        violations = _serious_violations(page)
        contrast = [item for item in violations if item["id"] == "color-contrast"]
        assert contrast, (
            "axe reported no contrast failure for a 1.2:1 element, so the "
            f"harness is not measuring anything. Full report: {violations}"
        )
        assert any(
            "deliberately-unreadable" in sample
            for item in contrast
            for sample in item["sample"]
        ), f"the contrast failure axe found is not the injected one: {contrast}"
