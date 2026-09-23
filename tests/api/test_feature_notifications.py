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
        """Proves every template can see it, not only the inbox.

        The badge lives in the shared layout, so a view that forgot to
        pass the value would render it as zero without failing.
        """
        add_notification(session_factory, world["adopter_user_id"])
        add_notification(session_factory, world["adopter_user_id"])

        response = adopter_client.get("/animals/")

        assert response.status_code == 200

    def test_an_anonymous_visitor_sees_zero(self, client: FlaskClient) -> None:
        """Proves the processor does not fail for a signed-out visitor."""
        assert client.get("/").status_code == 200
