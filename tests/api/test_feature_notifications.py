"""HTTP tests for the notification inbox (spec section 23).

Notifications were being written from the moment applications and
invitations existed, and there was nowhere to read them: an adopter whose
application was approved was told so by a database row nobody could see.

Ownership is the interesting part. The listing takes no identifier at all -
it queries on the signed-in user - so reading somebody else's inbox is not
expressible. The mark-read route has to take one from the URL, so the
handler proves the message belongs to the reader.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.domain.enums import NotificationType
from app.infrastructure.models import Notification, new_identifier
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.api


def add_notification(
    session_factory: sessionmaker[Session],
    user_id: str,
    *,
    title: str = "Your application was approved",
    is_read: bool = False,
    link_url: str | None = "/my/applications",
    minutes_ago: int = 5,
) -> str:
    """Insert one notification for a user and return its identifier."""
    notification_id = new_identifier()
    with session_factory() as session:
        session.add(
            Notification(
                notification_id=notification_id,
                user_id=user_id,
                notification_type=NotificationType.APPLICATION_STATUS_CHANGED.value,
                title=title,
                body="A staff member has made a decision.",
                link_url=link_url,
                is_read=is_read,
                created_at=datetime.now(UTC).replace(tzinfo=None)
                - timedelta(minutes=minutes_ago),
            )
        )
        session.commit()
    return notification_id


def is_read(session_factory: sessionmaker[Session], notification_id: str) -> bool:
    """Read back one notification's read flag."""
    with session_factory() as session:
        return bool(
            session.execute(
                select(Notification.is_read).where(
                    Notification.notification_id == notification_id
                )
            ).scalar_one()
        )


class TestTheInbox:
    """An adopter can read the messages written for them."""

    def test_a_notification_is_shown(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves a written notification is now readable."""
        add_notification(session_factory, world["adopter_user_id"])

        body = adopter_client.get("/my/notifications").get_data(as_text=True)

        assert "Your application was approved" in body

    def test_an_empty_inbox_explains_itself(
        self, adopter_client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves nothing-to-show is a message rather than a blank page."""
        body = adopter_client.get("/my/notifications").get_data(as_text=True)

        assert "Nothing here yet" in body

    def test_staff_have_an_inbox_too(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the inbox is not adopter-only.

        Staff receive messages as well, and an inbox one role cannot open
        is worse than no inbox.
        """
        add_notification(
            session_factory, world["staff_user_id"], title="An invitation was accepted"
        )

        body = staff_client.get("/my/notifications").get_data(as_text=True)

        assert "An invitation was accepted" in body

    def test_an_anonymous_visitor_is_sent_to_sign_in(self, client: FlaskClient) -> None:
        """Proves the inbox is not public."""
        assert client.get("/my/notifications").status_code in (302, 401)


class TestOwnership:
    """One account's messages are not another's."""

    def test_the_inbox_shows_only_your_own_messages(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the listing is scoped to the signed-in user."""
        add_notification(
            session_factory, world["other_user_id"], title="Somebody else's news"
        )

        body = adopter_client.get("/my/notifications").get_data(as_text=True)

        assert "Somebody else's news" not in body

    def test_marking_another_users_message_read_is_refused(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the identifier in the URL is not trusted.

        This route has to take one, so the handler checks it belongs to
        the reader rather than assuming the interface only ever offers
        their own.
        """
        theirs = add_notification(session_factory, world["other_user_id"])

        response = adopter_client.post(f"/my/notifications/{theirs}/read")

        assert response.status_code == 403
        assert is_read(session_factory, theirs) is False

    def test_marking_all_read_does_not_touch_another_user(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the bulk action is scoped too."""
        mine = add_notification(session_factory, world["adopter_user_id"])
        theirs = add_notification(session_factory, world["other_user_id"])

        adopter_client.post("/my/notifications/read-all")

        assert is_read(session_factory, mine) is True
        assert is_read(session_factory, theirs) is False


class TestMarkingRead:
    """Reading a message records that it was read."""

    def test_marking_one_read_records_it(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the flag is persisted."""
        notification_id = add_notification(session_factory, world["adopter_user_id"])

        adopter_client.post(f"/my/notifications/{notification_id}/read")

        assert is_read(session_factory, notification_id) is True

    def test_opening_a_message_follows_its_link(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the message takes the reader where it is about."""
        notification_id = add_notification(session_factory, world["adopter_user_id"])

        response = adopter_client.post(
            f"/my/notifications/{notification_id}/read",
            data={"target_url": "/my/applications"},
        )

        assert response.headers["Location"].endswith("/my/applications")

    def test_an_off_site_target_is_refused(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the redirect target cannot be turned into an open redirect.

        The value arrives in a form field, so it is attacker-controlled in
        exactly the way the `next` parameter on sign-in was.
        """
        notification_id = add_notification(session_factory, world["adopter_user_id"])

        response = adopter_client.post(
            f"/my/notifications/{notification_id}/read",
            data={"target_url": "//evil.example.com"},
        )

        assert not response.headers["Location"].startswith("//evil")

    def test_a_backslash_target_is_refused_too(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the spelling this route used to miss is refused.

        The inline check here tested only for a leading "//". Some browsers
        normalise a backslash to a forward slash, so a slash followed by a
        backslash and a host was an off-site address that passed. Both routes
        now share the one guard in app/controllers/helpers.py, which parses
        the value instead of inspecting its first two characters.
        """
        notification_id = add_notification(session_factory, world["adopter_user_id"])

        response = adopter_client.post(
            f"/my/notifications/{notification_id}/read",
            data={"target_url": "/\\evil.example.com"},
        )

        assert "evil.example.com" not in response.headers["Location"]

    def test_marking_an_unknown_message_read_is_a_404(
        self, adopter_client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves a bad identifier is reported rather than ignored."""
        response = adopter_client.post(f"/my/notifications/{new_identifier()}/read")

        assert response.status_code == 404


class TestTheUnreadCount:
    """The badge in the layout comes from a context processor."""

    def test_the_count_is_available_on_any_page(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the count reaches the layout and is drawn there.

        It used to reach only as far as the context: the processor ran a
        query on every authenticated request and no template rendered the
        answer, so the cost was paid and the badge did not exist.
        """
        add_notification(session_factory, world["adopter_user_id"])
        add_notification(session_factory, world["adopter_user_id"])

        response = adopter_client.get("/animals/")
        body = response.get_data(as_text=True)

        assert response.status_code == 200
        assert 'class="badge"' in body
        assert ">2<" in body

    def test_a_fully_read_inbox_shows_no_badge(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves a zero count draws nothing rather than a badge reading 0.

        Negative half of the pair: a permanent badge would stop meaning
        "there is something new".
        """
        add_notification(session_factory, world["adopter_user_id"], is_read=True)

        body = adopter_client.get("/animals/").get_data(as_text=True)

        assert 'class="badge"' not in body

    def test_both_roles_can_reach_the_inbox_from_the_layout(
        self, adopter_client: FlaskClient, staff_client: FlaskClient
    ) -> None:
        """Proves the inbox is reachable without typing its URL.

        There was no link to it anywhere in the navigation, for either role.
        """
        for signed_in in (adopter_client, staff_client):
            body = signed_in.get("/animals/").get_data(as_text=True)
            assert "/my/notifications" in body

    def test_an_anonymous_visitor_sees_zero(self, client: FlaskClient) -> None:
        """Proves the processor does not fail for a signed-out visitor.

        And that the new link is inside the authenticated branch: a signed-out
        visitor has no inbox, so offering one would send them to sign in for
        a page they did not ask for.
        """
        response = client.get("/")
        body = response.get_data(as_text=True)

        assert response.status_code == 200
        assert "/my/notifications" not in body
        assert 'class="badge"' not in body


class TestTheInboxGrouping:
    """The split into "last 24 hours" and "earlier" is the view model's.

    The template used to decide it by searching the relative label for the
    words "day", "month" and "year". That was a decision taken in a view
    (rule R2), and it happened to be right only because this module carried
    its own time vocabulary: the shared formatter says "on 23 Sep 2026" past
    a week, which contains none of the three words, so a month-old message
    would have been filed under "last 24 hours".
    """

    def test_a_message_from_this_hour_is_grouped_as_recent(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves a fresh message appears under the last-24-hours heading."""
        add_notification(session_factory, world["adopter_user_id"], minutes_ago=30)

        body = adopter_client.get("/my/notifications").get_data(as_text=True)

        assert "Last 24 hours" in body
        assert "Earlier" not in body

    def test_a_month_old_message_is_grouped_as_earlier(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the regression the shared formatter would have introduced.

        Negative half of the pair: this is the case the substring grouping
        got wrong, and it is grouped from `created_at` now rather than from
        any wording.
        """
        add_notification(
            session_factory,
            world["adopter_user_id"],
            minutes_ago=60 * 24 * 30,
        )

        body = adopter_client.get("/my/notifications").get_data(as_text=True)

        assert "Earlier" in body
        assert "Last 24 hours" not in body

    def test_the_inbox_uses_the_shared_relative_wording(
        self,
        session_factory: sessionmaker[Session],
        world: dict[str, str],
    ) -> None:
        """Proves the inbox and every other screen phrase a timestamp alike.

        This module had a second implementation with a different vocabulary,
        so the same age read "2 hours ago" here and elsewhere by coincidence
        and "1 month ago" against "on 23 Sep 2026" when it did not.
        """
        from app.cqrs.queries.formatting import describe_relative_time
        from app.cqrs.queries.notification_queries import (
            ListMyNotificationsHandler,
            ListMyNotificationsQuery,
        )

        add_notification(session_factory, world["adopter_user_id"], minutes_ago=120)

        with session_factory() as session:
            items = ListMyNotificationsHandler().handle(
                ListMyNotificationsQuery(user_id=world["adopter_user_id"]), session
            )

        assert items
        for item in items:
            assert item.relative_label == describe_relative_time(item.created_at)
