"""End-to-end browser journeys (blueprint section 17, spec section 28).

Each test walks a complete user process in a real browser against the real
cloud database, covering the demo scenarios spec section 28 lists.

Assertions are about *behaviour reaching the screen*, not about wording the
language model produced (rule R3). Where the agent's prose is involved, the
test asserts the score and the structure are present, never the sentences.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import ADOPTER_EMAIL, STAFF_EMAIL, sign_in

pytestmark = pytest.mark.e2e


class TestPublicBrowsing:
    """Scenario: anyone can find an animal without an account."""

    def test_visitor_searches_filters_and_opens_an_animal(
        self, page: Page, live_server: str
    ) -> None:
        """Proves blueprint 4.1 and 4.2: search, filter, then view details.

        Walked as a visitor with no account, because browsing is the first
        thing anybody does and it must not require signing up.
        """
        page.goto(live_server, wait_until="load")

        # Each click below waits on the *destination URL* rather than on a
        # load state. `wait_for_load_state` reports on whichever document is
        # current, so when a click lands while a previous navigation is still
        # settling it can answer about the old page - and then time out.
        # Scoping the selector to the navigation bar also matters: the hero
        # carries a "Browse 32 animals" button, so a bare text selector
        # matches two elements and Playwright waits for it to become unique.
        page.locator(".topbar__nav a", has_text="Browse").click()
        page.wait_for_url(lambda url: url.rstrip("/").endswith("/animals"))

        assert page.locator(".animal-card").count() > 0, "search returned nothing"

        page.select_option("#species", "CAT")
        page.click("button:has-text('Apply filters')")
        # A predicate rather than a glob: Playwright's URL globs match the
        # path reliably but not query strings, and this navigation differs
        # from the previous page only in its query.
        page.wait_for_url(lambda url: "species=CAT" in url)

        # Every remaining card must be a cat, not merely fewer cards.
        card_text = page.locator(".animal-card").all_inner_texts()
        assert card_text, "filtering removed every result"
        assert all("Cat" in text for text in card_text)

        first_card_url = page.locator(".animal-card").first.get_attribute("href")
        assert first_card_url is not None, "an animal card must link somewhere"
        page.locator(".animal-card").first.click()
        page.wait_for_url(lambda url: first_card_url in url)

        expect(page.locator("h1")).to_be_visible()
        expect(page.locator(".detail-photo")).to_be_visible()
        expect(page.locator(".spec-list")).to_be_visible()

    def test_empty_search_explains_how_to_widen_it(
        self, page: Page, live_server: str
    ) -> None:
        """Proves a zero-result search guides rather than dead-ends."""
        page.goto(
            f"{live_server}/animals/?q=zzzznothingmatchesthis", wait_until="load"
        )

        expect(page.locator(".empty")).to_be_visible()
        expect(page.locator("text=Clear filters")).to_be_visible()


class TestAdopterJourney:
    """Scenario: an adopter signs in, sees matches and applies."""

    def test_adopter_sees_ranked_matches_with_scores(
        self, page: Page, live_server: str
    ) -> None:
        """Proves Find My Pet ranks animals from the stored profile (spec 6.1).

        Asserts a numeric score is on screen, not what the explanation says:
        the number is deterministic, the prose is not.
        """
        sign_in(page, live_server, ADOPTER_EMAIL)
        page.goto(f"{live_server}/my/matches", wait_until="load")

        matches = page.locator(".candidate")
        assert matches.count() > 0, "no matches were produced"

        first_score = page.locator(".score__value").first.inner_text()
        assert first_score.strip().isdigit()
        assert 0 <= int(first_score) <= 100

    def test_matches_are_ordered_best_first(
        self, page: Page, live_server: str
    ) -> None:
        """Proves the ranking really is ranked."""
        sign_in(page, live_server, ADOPTER_EMAIL)
        page.goto(f"{live_server}/my/matches", wait_until="load")

        scores = [
            int(value)
            for value in page.locator(".score__value").all_inner_texts()
            if value.strip().isdigit()
        ]
        assert scores == sorted(scores, reverse=True)

    def test_adopter_reaches_their_own_area(
        self, page: Page, live_server: str
    ) -> None:
        """Proves the adopter's personal pages all render."""
        sign_in(page, live_server, ADOPTER_EMAIL)

        for path, heading in (
            ("/my/applications", "My applications"),
            ("/my/invitations", "Invitations"),
            ("/my/profile", "My adoption profile"),
        ):
            page.goto(f"{live_server}{path}", wait_until="load")
            expect(page.locator("h1")).to_contain_text(heading)

    def test_profile_form_rejects_children_without_an_age(
        self, page: Page, live_server: str
    ) -> None:
        """Proves server-side validation surfaces in the interface.

        The age field carries no `required` attribute, because whether it is
        required depends on another field. So this is the server's check
        reaching the user, which is what spec section 21 asks for.
        """
        sign_in(page, live_server, ADOPTER_EMAIL)
        page.goto(f"{live_server}/my/profile", wait_until="load")

        page.check("input[name=household_has_children]")
        page.fill("#youngest_child_age", "")
        page.click("button:has-text('Save profile')")

        # The summary is found by its role rather than its class, so a restyle
        # cannot break the proof that the server's check reaches the screen.
        expect(page.locator("[role=alert]")).to_be_visible()
        expect(page.locator(".field__error")).to_be_visible()


class TestStaffJourney:
    """Scenario: staff review the roster, rankings and dashboard."""

    def test_staff_dashboard_shows_operational_figures(
        self, page: Page, live_server: str
    ) -> None:
        """Proves blueprint 4.4: a dashboard with real numbers and actions."""
        sign_in(page, live_server, STAFF_EMAIL)
        page.goto(f"{live_server}/dashboard", wait_until="load")

        expect(page.locator("h1")).to_contain_text("Dashboard")
        assert page.locator(".stat").count() >= 6

        # Every tile must carry an actual figure.
        for value in page.locator(".stat__value").all_inner_texts():
            assert value.strip().isdigit()

    def test_dashboard_activity_feed_is_populated(
        self, page: Page, live_server: str
    ) -> None:
        """Proves the feed reads real events from the log (FR-6.4)."""
        sign_in(page, live_server, STAFF_EMAIL)
        page.goto(f"{live_server}/dashboard", wait_until="load")

        assert page.locator(".activity__item").count() > 0

    def test_staff_animal_table_lists_the_roster(
        self, page: Page, live_server: str
    ) -> None:
        """Proves blueprint 4.3: the tabular display."""
        sign_in(page, live_server, STAFF_EMAIL)
        page.goto(f"{live_server}/animals/manage", wait_until="load")

        assert page.locator("tbody tr").count() > 0
        expect(page.locator("thead")).to_contain_text("Status")

    def test_staff_ranks_applicants_for_an_animal(
        self, page: Page, live_server: str
    ) -> None:
        """Proves Find My Adopter reaches a ranking (spec 7.2).

        Walks in through the management table rather than a known URL, so
        the navigation path is tested too.
        """
        sign_in(page, live_server, STAFF_EMAIL)
        page.goto(f"{live_server}/animals/manage", wait_until="load")

        page.locator("tbody a", has_text="Rank").first.click()
        page.wait_for_url("**/adopters")

        expect(page.locator("h1")).to_contain_text("applicants")
        # Either candidates or an explanatory empty state, never a blank page.
        assert page.locator(".candidate").count() > 0 or page.locator(".empty").count() > 0

    def test_discovery_finds_adopters_who_did_not_apply(
        self, page: Page, live_server: str
    ) -> None:
        """Proves Find More Adopters is distinct from applicant ranking (spec 7.3)."""
        sign_in(page, live_server, STAFF_EMAIL)
        page.goto(f"{live_server}/animals/manage", wait_until="load")

        first_rank_link = page.locator("tbody a", has_text="Rank").first.get_attribute("href")
        assert first_rank_link is not None

        page.goto(
            f"{live_server}{first_rank_link.replace('/adopters', '/discover')}",
            wait_until="load",
        )

        expect(page.locator("h1")).to_contain_text("Suggested adopters")
        for text in page.locator(".candidate .tag--brand").all_inner_texts():
            assert "not applied" in text.lower()

    def test_rankings_state_that_a_human_decides(
        self, page: Page, live_server: str
    ) -> None:
        """Proves spec 6.4 is visible to staff, not only enforced in code.

        The limitation matters most at the moment somebody is about to act
        on a score, so it belongs on the ranking page.
        """
        sign_in(page, live_server, STAFF_EMAIL)
        page.goto(f"{live_server}/animals/manage", wait_until="load")
        page.locator("tbody a", has_text="Rank").first.click()
        page.wait_for_url("**/adopters")

        banner = page.locator(".alert--info").inner_text().lower()
        assert "not generated by a language model" in banner
        assert "final decision" in banner


class TestAuthorizationInTheBrowser:
    """Permissions hold for a real browser session, not only for a test client."""

    def test_adopter_cannot_reach_the_dashboard(
        self, page: Page, live_server: str
    ) -> None:
        """Proves an adopter typing the URL is refused."""
        sign_in(page, live_server, ADOPTER_EMAIL)
        response = page.goto(f"{live_server}/dashboard", wait_until="load")

        assert response is not None
        assert response.status == 403

    def test_adopter_navigation_offers_no_staff_links(
        self, page: Page, live_server: str
    ) -> None:
        """Proves the interface matches the permissions.

        The server refusal is the control; this checks the interface is not
        dangling links that would only produce a 403.
        """
        sign_in(page, live_server, ADOPTER_EMAIL)
        page.goto(live_server, wait_until="load")

        navigation = page.locator(".topbar__nav").inner_text()
        assert "Dashboard" not in navigation
        assert "Manage" not in navigation

    def test_staff_navigation_offers_no_adopter_links(
        self, page: Page, live_server: str
    ) -> None:
        """Proves the same in the other direction."""
        sign_in(page, live_server, STAFF_EMAIL)
        page.goto(live_server, wait_until="load")

        navigation = page.locator(".topbar__nav").inner_text()
        assert "My matches" not in navigation
        assert "Invitations" not in navigation


class TestInterfaceQuality:
    """The interface holds together at the edges (blueprint section 13)."""

    def test_every_animal_card_shows_an_image(
        self, page: Page, live_server: str
    ) -> None:
        """Proves spec 24: images are mandatory, not decorative.

        A missing photograph must fall back to the styled placeholder rather
        than a broken image icon.
        """
        page.goto(f"{live_server}/animals/", wait_until="load")

        cards = page.locator(".animal-card")
        for index in range(cards.count()):
            media = cards.nth(index).locator(".animal-card__media")
            has_photo = media.locator("img").count() > 0
            has_placeholder = media.locator(".animal-card__placeholder").count() > 0
            assert has_photo or has_placeholder

    def test_layout_survives_a_phone_viewport(
        self, page: Page, live_server: str
    ) -> None:
        """Proves the page does not scroll horizontally at 320px (NFR-6.1)."""
        page.set_viewport_size({"width": 320, "height": 720})
        page.goto(f"{live_server}/animals/", wait_until="load")

        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 1, f"page overflows by {overflow}px at 320 wide"

    def test_dark_mode_renders(self, page: Page, live_server: str) -> None:
        """Proves the dark theme is wired up, not just declared (NFR-6.5)."""
        page.emulate_media(color_scheme="dark")
        page.goto(f"{live_server}/animals/", wait_until="load")

        background = page.evaluate(
            "() => getComputedStyle(document.body).backgroundColor"
        )
        assert background not in ("rgba(0, 0, 0, 0)", ""), "body has no background"

    def test_a_missing_page_renders_a_friendly_error(
        self, page: Page, live_server: str
    ) -> None:
        """Proves a 404 is a designed page rather than a stack trace."""
        response = page.goto(f"{live_server}/animals/nope", wait_until="load")

        assert response is not None
        assert response.status == 404
        expect(page.locator(".empty")).to_be_visible()
        assert "Traceback" not in page.content()
