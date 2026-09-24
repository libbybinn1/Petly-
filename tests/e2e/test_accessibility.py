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
from playwright.sync_api import Dialog, Page

from tests.e2e.conftest import ADOPTER_EMAIL, STAFF_EMAIL, sign_in

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

# Screens that need an account. Each entry is (email, path, name): the role
# is part of the case, because the same URL renders different markup for an
# adopter and for staff, and only one of the two would otherwise be checked.
SIGNED_IN_SCREENS = [
    (ADOPTER_EMAIL, "/my/profile", "adopter profile"),
    (ADOPTER_EMAIL, "/my/invitations", "my invitations"),
    (ADOPTER_EMAIL, "/my/notifications", "notification inbox"),
    (STAFF_EMAIL, "/animals/manage", "animal management"),
    (STAFF_EMAIL, "/animals/new", "the animal form"),
]

DETAILS_ROLES = [(ADOPTER_EMAIL, "adopter"), (STAFF_EMAIL, "staff")]


def _first_animal_path(page: Page, live_server: str) -> str:
    """Find a real animal's details path by following the browse screen.

    Hard-coding an identifier would tie the suite to the seed data, and a
    details page is exactly where the seed is most likely to change.
    """
    page.goto(f"{live_server}/animals/", wait_until="load")
    href = page.locator("a.animal-card").first.get_attribute("href")
    assert href, "the browse screen offered no animal to open"
    return href


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

    reported = page.evaluate(
        """async () => {
             const report = await axe.run(document, {resultTypes: ['violations']});
             return report.violations.map(v => ({
               id: v.id, impact: v.impact, nodes: v.nodes.length,
               help: v.help,
               sample: v.nodes.slice(0, 3).map(n => n.html.slice(0, 200))
             }));
           }"""
    )
    # `page.evaluate` is typed `Any`, so without this the whole file would
    # type-check against nothing. Narrowing here is also a real check: a
    # changed axe API would return something other than a list of objects,
    # and silently producing an empty result would read as a clean page.
    if not isinstance(reported, list):
        raise RuntimeError(f"axe-core returned {type(reported).__name__}, not a list")
    return [violation for violation in reported if isinstance(violation, dict)]


def _serious_violations(page: Page) -> list[dict[str, object]]:
    """The violations that fail a test: serious and critical only."""
    return [item for item in _run_axe(page) if item["impact"] in SERIOUS]


def _samples(violation: dict[str, object]) -> list[str]:
    """The markup axe recorded for a violation, as strings.

    A dictionary read out of the browser is `dict[str, object]`, and an
    `object` cannot be iterated, so the list is narrowed here once rather
    than asserted about in place.
    """
    recorded = violation.get("sample")
    return [str(item) for item in recorded] if isinstance(recorded, list) else []


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


class TestSignedInScreensPassAxe:
    """The screens behind a sign-in, which the anonymous sweep cannot reach.

    These are the screens the design system does *not* own end to end: a long
    form, a nine-column roster, a details page that changes shape by role, and
    two list screens. That is where per-screen markup accumulates, and where
    the first audit found every heading-order and empty-table-header failure.
    """

    @pytest.mark.parametrize(("email", "path", "name"), SIGNED_IN_SCREENS)
    def test_screen_has_no_serious_violations(
        self, page: Page, live_server: str, email: str, path: str, name: str
    ) -> None:
        """Proves the signed-in screen has no serious or critical violation.

        Covers heading order, the table's column names, the form's labels and
        the landmark structure in one measurement: each of those is a rule
        axe can check, so a template that loses one fails here.
        """
        sign_in(page, live_server, email)
        page.goto(f"{live_server}{path}", wait_until="load")
        violations = _serious_violations(page)
        assert not violations, f"{name} ({path}) has serious axe violations: {violations}"

    @pytest.mark.parametrize(("email", "path", "name"), SIGNED_IN_SCREENS)
    def test_screen_passes_in_dark_mode_too(
        self, page: Page, live_server: str, email: str, path: str, name: str
    ) -> None:
        """Proves the dark theme is checked on these screens, not assumed.

        Dark mode is a token override rather than a second stylesheet
        (docs/UX.md section 2), so a tint that reads in one theme can fail in
        the other - which is what happened to the primary button, at 3.74:1
        light and 2.80:1 dark.
        """
        page.emulate_media(color_scheme="dark")
        sign_in(page, live_server, email)
        page.goto(f"{live_server}{path}", wait_until="load")
        violations = _serious_violations(page)
        assert not violations, f"{name} ({path}) fails in dark mode: {violations}"

    @pytest.mark.parametrize(("email", "role"), DETAILS_ROLES)
    def test_animal_details_passes_for_every_role(
        self, page: Page, live_server: str, email: str, role: str
    ) -> None:
        """Proves the details screen is clean for both kinds of account.

        It renders three different lower halves - an application form, a pair
        of staff panels, or a prompt to sign in - so checking one role would
        leave two thirds of the screen unmeasured. It is also the screen that
        jumped `h1` to `h3` before this pass.
        """
        animal_path = _first_animal_path(page, live_server)
        sign_in(page, live_server, email)
        page.goto(f"{live_server}{animal_path}", wait_until="load")
        violations = _serious_violations(page)
        assert not violations, f"animal details as {role} has violations: {violations}"


class TestPerScreenMarkupPromises:
    """Claims from docs/UX.md sections 6 and 9 that axe cannot measure."""

    def test_the_roster_table_names_every_column(
        self, page: Page, live_server: str
    ) -> None:
        """Proves no column header is blank and the table has a caption.

        The thumbnail and action columns had empty `<th>` elements, so two of
        the nine columns were announced as nothing at all. The caption is what
        says which table this is before any of it is read out.
        """
        sign_in(page, live_server, STAFF_EMAIL)
        page.goto(f"{live_server}/animals/manage", wait_until="load")

        blank_headers = page.evaluate(
            """() => [...document.querySelectorAll('.table th')]
                       .filter(cell => !cell.textContent.trim()).length"""
        )
        assert blank_headers == 0
        assert page.locator("table caption.sr-only").count() == 1

    def test_every_roster_cell_carries_its_column_name_on_a_phone(
        self, page: Page, live_server: str
    ) -> None:
        """Proves the card layout cannot lose a cell's header.

        Below 760px `.table--cards` hides `<thead>` and prints each cell's own
        `data-label` instead. A data cell without one would simply lose its
        header, which is worse than the horizontal scroll it replaced.
        """
        page.set_viewport_size({"width": 375, "height": 780})
        sign_in(page, live_server, STAFF_EMAIL)
        page.goto(f"{live_server}/animals/manage", wait_until="load")

        unlabelled = page.evaluate(
            """() => [...document.querySelectorAll('.table--cards tbody td')]
                       .filter(cell => !cell.hasAttribute('data-label')
                                       && !cell.querySelector('.table__thumb, .table__actions'))
                       .length"""
        )
        assert unlabelled == 0, "a data cell would lose its column name in card layout"

    def test_a_rejected_profile_lists_its_errors_as_links_to_the_fields(
        self, page: Page, live_server: str
    ) -> None:
        """Proves the error summary points at controls that exist.

        Negative test (rule R3): it submits a profile the server must refuse.
        A summary that says "please fix 2 problems below" leaves the reader to
        go and find them, and a summary whose links point at nothing is worse
        than none - so every link is resolved against the document.
        """
        sign_in(page, live_server, ADOPTER_EMAIL)
        page.goto(f"{live_server}/my/profile", wait_until="load")

        # Defeat the browser's own validation so the server gets to answer.
        page.evaluate("() => { document.querySelector('form.card').noValidate = true; }")
        page.fill("#city", "")
        page.fill("#daily_hours_available", "99")
        page.click("form.card button[type=submit]")
        page.wait_for_load_state("load")

        links = page.locator(".error-summary a")
        assert links.count() >= 2, "the summary did not list both refusals"

        for index in range(links.count()):
            target = links.nth(index).get_attribute("href") or ""
            assert target.startswith("#")
            assert page.locator(target).count() == 1, f"{target} points at nothing"

        assert page.locator('[aria-invalid="true"]').count() >= 2, (
            "the refused fields are not marked invalid"
        )

    def test_a_dependent_field_is_revealed_by_the_answer_above_it(
        self, page: Page, live_server: str
    ) -> None:
        """Proves progressive disclosure follows its checkbox, both ways.

        The region is hidden only because JavaScript hid it; the guarantee
        that matters is that ticking the box brings it back, because a field
        the server requires must never become unreachable.
        """
        sign_in(page, live_server, ADOPTER_EMAIL)
        page.goto(f"{live_server}/my/profile", wait_until="load")

        region = page.locator('[data-shown-by="household_has_children"]')
        assert region.is_hidden(), "the dependent field is shown before it applies"
        page.check("#household_has_children")
        assert region.is_visible(), "ticking the box did not reveal the field it governs"
        page.uncheck("#household_has_children")
        assert region.is_hidden()

    def test_declining_an_invitation_asks_first(
        self, page: Page, live_server: str
    ) -> None:
        """Proves the destructive answer is confirmed and can be called off.

        Declining cannot be undone and the invitation does not come back. The
        confirmation is a convenience rather than the authority, so the test
        dismisses it and requires that nothing was submitted.
        """
        sign_in(page, live_server, ADOPTER_EMAIL)
        page.goto(f"{live_server}/my/invitations", wait_until="load")

        decline = page.locator("form[data-confirm] button")
        assert decline.count() >= 1, "the seed has no open invitation to decline"
        assert "btn--danger-ghost" in (decline.first.get_attribute("class") or "")

        asked: list[str] = []

        def record_and_dismiss(dialog: Dialog) -> None:
            """Note the question, then answer "cancel" so nothing is submitted."""
            asked.append(dialog.message)
            dialog.dismiss()

        page.on("dialog", record_and_dismiss)
        before = page.url
        decline.first.click()
        page.wait_for_timeout(500)

        assert asked, "declining submitted without asking"
        assert page.url == before, "the form submitted after the confirmation was dismissed"


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
            for sample in _samples(item)
        ), f"the contrast failure axe found is not the injected one: {contrast}"
