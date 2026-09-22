"""SQLAlchemy ORM models mapping the schema in docs/MODEL_DATA.md.

Engine target is Microsoft SQL Server 2014 Express, which imposes constraints
that shaped these definitions:

- No native JSON type (added in 2016). Every structured payload is stored as
  NVARCHAR(MAX) and serialized in Python. Never use JSON_VALUE or OPENJSON.
- Identifiers are application-generated UUIDs rather than IDENTITY columns,
  because event sourcing needs an aggregate id to exist before its first event
  is appended.
- Images are files on disk; only their URL is stored, keeping the free-tier
  database well under its size cap.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import (
    ActivityLevel,
    AggregateType,
    AnalysisJobStatus,
    AnalysisJobType,
    AnimalSize,
    AnimalStatus,
    ApplicationStatus,
    ExperienceLevel,
    HomeType,
    InvitationStatus,
    MatchDirection,
    NotificationType,
    Species,
    Temperament,
    UserRole,
)
from app.infrastructure.database import Base

# SQL Server has no dedicated UUID column type in SQLAlchemy's mssql dialect
# that round-trips cleanly across drivers, so identifiers are stored as
# 36-character strings. Comparisons stay exact and indexes remain usable.
UUID_LENGTH = 36


def new_identifier() -> str:
    """Generate a fresh aggregate identifier."""
    return str(uuid.uuid4())


def _enum_check(column_name: str, enum_class: type) -> CheckConstraint:
    """Build a CHECK constraint restricting a column to an enum's values.

    The database enforces the same vocabulary the Python enum does, so a bad
    value cannot enter through a route that bypasses the ORM.
    """
    allowed = ", ".join(f"'{member.value}'" for member in enum_class)
    return CheckConstraint(f"{column_name} IN ({allowed})", name=column_name)


class User(Base):
    """An account. Either an adopter or a member of organization staff."""

    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(
        String(UUID_LENGTH), primary_key=True, default=new_identifier
    )
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(150), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    adopter_profile: Mapped[AdopterProfile | None] = relationship(
        back_populates="user", uselist=False
    )

    __table_args__ = (_enum_check("role", UserRole),)


class AdopterProfile(Base):
    """Stable facts about an adopter and their living situation (spec 5.1)."""

    __tablename__ = "adopter_profiles"

    adopter_profile_id: Mapped[str] = mapped_column(
        String(UUID_LENGTH), primary_key=True, default=new_identifier
    )
    user_id: Mapped[str] = mapped_column(
        String(UUID_LENGTH), ForeignKey("users.user_id"), nullable=False, unique=True
    )

    home_type: Mapped[str] = mapped_column(String(20), nullable=False)
    has_yard: Mapped[bool] = mapped_column(nullable=False, default=False)
    yard_size_sqm: Mapped[int | None] = mapped_column(Integer, nullable=True)

    household_has_children: Mapped[bool] = mapped_column(nullable=False, default=False)
    youngest_child_age: Mapped[int | None] = mapped_column(Integer, nullable=True)

    has_other_animals: Mapped[bool] = mapped_column(nullable=False, default=False)
    other_animals_description: Mapped[str | None] = mapped_column(String(500), nullable=True)

    experience_level: Mapped[str] = mapped_column(String(20), nullable=False)
    activity_level: Mapped[str] = mapped_column(String(20), nullable=False)
    daily_hours_available: Mapped[float] = mapped_column(Numeric(4, 1), nullable=False)
    city: Mapped[str] = mapped_column(String(100), nullable=False)

    preferred_species: Mapped[str | None] = mapped_column(String(200), nullable=True)
    preferred_size: Mapped[str | None] = mapped_column(String(20), nullable=True)
    preferred_age_range: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Required by spec 5.1 and a hard eligibility gate for Find More Adopters.
    open_to_proactive_suggestions: Mapped[bool] = mapped_column(nullable=False, default=False)
    is_complete: Mapped[bool] = mapped_column(nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    user: Mapped[User] = relationship(back_populates="adopter_profile")

    __table_args__ = (
        _enum_check("home_type", HomeType),
        _enum_check("experience_level", ExperienceLevel),
        _enum_check("activity_level", ActivityLevel),
        CheckConstraint("yard_size_sqm IS NULL OR yard_size_sqm >= 0", name="yard_size"),
        CheckConstraint(
            "youngest_child_age IS NULL OR (youngest_child_age >= 0 AND youngest_child_age <= 18)",
            name="child_age",
        ),
        CheckConstraint(
            "daily_hours_available >= 0 AND daily_hours_available <= 24",
            name="daily_hours",
        ),
    )


class Animal(Base):
    """An animal available for adoption (spec 5.2)."""

    __tablename__ = "animals"

    animal_id: Mapped[str] = mapped_column(
        String(UUID_LENGTH), primary_key=True, default=new_identifier
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    species: Mapped[str] = mapped_column(String(30), nullable=False)
    breed: Mapped[str | None] = mapped_column(String(100), nullable=True)
    age_years: Mapped[float] = mapped_column(Numeric(4, 1), nullable=False)
    size: Mapped[str] = mapped_column(String(20), nullable=False)
    temperament: Mapped[str] = mapped_column(String(30), nullable=False)
    activity_level: Mapped[str] = mapped_column(String(20), nullable=False)

    good_with_children: Mapped[bool] = mapped_column(nullable=False, default=True)
    good_with_other_animals: Mapped[bool] = mapped_column(nullable=False, default=True)
    has_special_needs: Mapped[bool] = mapped_column(nullable=False, default=False)
    special_needs_description: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    required_space: Mapped[str] = mapped_column(String(20), nullable=False)
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    images: Mapped[list[AnimalImage]] = relationship(
        back_populates="animal", cascade="all, delete-orphan", order_by="AnimalImage.display_order"
    )

    __table_args__ = (
        _enum_check("species", Species),
        _enum_check("size", AnimalSize),
        _enum_check("temperament", Temperament),
        _enum_check("activity_level", ActivityLevel),
        _enum_check("required_space", AnimalSize),
        _enum_check("status", AnimalStatus),
        CheckConstraint("age_years >= 0", name="age_non_negative"),
        Index("ix_animals_status_species", "status", "species"),
        Index("ix_animals_city", "city"),
    )

    @property
    def primary_image_url(self) -> str | None:
        """URL of the animal's main photo, or None when it has no images.

        Spec 24 makes at least one image mandatory; this returns None rather
        than raising so a partially-seeded record cannot break a listing page.
        """
        if not self.images:
            return None
        for image in self.images:
            if image.is_primary:
                return image.image_url
        return self.images[0].image_url


class AnimalImage(Base):
    """A photo of an animal. Spec 24 requires at least one per animal."""

    __tablename__ = "animal_images"

    animal_image_id: Mapped[str] = mapped_column(
        String(UUID_LENGTH), primary_key=True, default=new_identifier
    )
    animal_id: Mapped[str] = mapped_column(
        String(UUID_LENGTH), ForeignKey("animals.animal_id", ondelete="CASCADE"), nullable=False
    )
    image_url: Mapped[str] = mapped_column(String(500), nullable=False)
    is_primary: Mapped[bool] = mapped_column(nullable=False, default=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    animal: Mapped[Animal] = relationship(back_populates="images")

    __table_args__ = (Index("ix_animal_images_animal", "animal_id"),)


class AdoptionApplication(Base):
    """One adopter's request to adopt one animal (spec 5.3)."""

    __tablename__ = "adoption_applications"

    application_id: Mapped[str] = mapped_column(
        String(UUID_LENGTH), primary_key=True, default=new_identifier
    )
    adopter_profile_id: Mapped[str] = mapped_column(
        String(UUID_LENGTH), ForeignKey("adopter_profiles.adopter_profile_id"), nullable=False
    )
    animal_id: Mapped[str] = mapped_column(
        String(UUID_LENGTH), ForeignKey("animals.animal_id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    applicant_message: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    originating_invitation_id: Mapped[str | None] = mapped_column(
        String(UUID_LENGTH), nullable=True
    )

    # Projection of ApplicationClosedDueToOtherApproval. Present for query
    # speed; the event log remains the source of truth. This is the column
    # that makes the spec 7.5 reopen rule mechanical rather than guesswork.
    closed_because_application_id: Mapped[str | None] = mapped_column(
        String(UUID_LENGTH), nullable=True
    )

    submitted_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by_user_id: Mapped[str | None] = mapped_column(
        String(UUID_LENGTH), ForeignKey("users.user_id"), nullable=True
    )

    animal: Mapped[Animal] = relationship()
    adopter_profile: Mapped[AdopterProfile] = relationship()

    __table_args__ = (
        _enum_check("status", ApplicationStatus),
        Index("ix_applications_animal_status", "animal_id", "status"),
        Index("ix_applications_adopter", "adopter_profile_id"),
    )


class AdoptionInvitation(Base):
    """A staff-initiated proactive suggestion with a 72-hour window (spec 5.4)."""

    __tablename__ = "adoption_invitations"

    invitation_id: Mapped[str] = mapped_column(
        String(UUID_LENGTH), primary_key=True, default=new_identifier
    )
    animal_id: Mapped[str] = mapped_column(
        String(UUID_LENGTH), ForeignKey("animals.animal_id"), nullable=False
    )
    adopter_profile_id: Mapped[str] = mapped_column(
        String(UUID_LENGTH), ForeignKey("adopter_profiles.adopter_profile_id"), nullable=False
    )
    sent_by_user_id: Mapped[str] = mapped_column(
        String(UUID_LENGTH), ForeignKey("users.user_id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    staff_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    sent_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    viewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    animal: Mapped[Animal] = relationship()
    adopter_profile: Mapped[AdopterProfile] = relationship()

    __table_args__ = (
        _enum_check("status", InvitationStatus),
        Index("ix_invitations_adopter_status", "adopter_profile_id", "status"),
        Index("ix_invitations_expires", "expires_at"),
    )


class MatchAnalysis(Base):
    """A stored compatibility assessment produced by the agent (spec 5.5).

    The JSON-shaped columns are NVARCHAR(MAX) because SQL Server 2014 has no
    JSON type. Repositories serialize and deserialize them.
    """

    __tablename__ = "match_analyses"

    match_analysis_id: Mapped[str] = mapped_column(
        String(UUID_LENGTH), primary_key=True, default=new_identifier
    )
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    adopter_profile_id: Mapped[str] = mapped_column(
        String(UUID_LENGTH), ForeignKey("adopter_profiles.adopter_profile_id"), nullable=False
    )
    animal_id: Mapped[str] = mapped_column(
        String(UUID_LENGTH), ForeignKey("animals.animal_id"), nullable=False
    )
    application_id: Mapped[str | None] = mapped_column(String(UUID_LENGTH), nullable=True)

    score: Mapped[int] = mapped_column(Integer, nullable=False)
    is_disqualified: Mapped[bool] = mapped_column(nullable=False, default=False)

    criterion_scores: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    reasons: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    concerns: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    missing_information: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    evidence_sources: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    used_web_search: Mapped[bool] = mapped_column(nullable=False, default=False)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    animal: Mapped[Animal] = relationship()
    adopter_profile: Mapped[AdopterProfile] = relationship()

    __table_args__ = (
        _enum_check("direction", MatchDirection),
        CheckConstraint("score >= 0 AND score <= 100", name="score_range"),
        Index("ix_analyses_animal_direction", "animal_id", "direction"),
        Index("ix_analyses_adopter_direction", "adopter_profile_id", "direction"),
    )


class Notification(Base):
    """An internal inbox message (spec 23). No email integration."""

    __tablename__ = "notifications"

    notification_id: Mapped[str] = mapped_column(
        String(UUID_LENGTH), primary_key=True, default=new_identifier
    )
    user_id: Mapped[str] = mapped_column(
        String(UUID_LENGTH), ForeignKey("users.user_id"), nullable=False
    )
    notification_type: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(String(1000), nullable=False)
    link_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    is_read: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        _enum_check("notification_type", NotificationType),
        Index("ix_notifications_user_read", "user_id", "is_read"),
    )


class DomainEvent(Base):
    """Append-only event log (architecture section 4).

    No UPDATE or DELETE is ever issued against this table. The repository
    exposes only append and read operations, so there is no update path to
    misuse.
    """

    __tablename__ = "domain_events"

    event_id: Mapped[str] = mapped_column(
        String(UUID_LENGTH), primary_key=True, default=new_identifier
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(50), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(UUID_LENGTH), nullable=False)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(String(UUID_LENGTH), nullable=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    __table_args__ = (
        _enum_check("aggregate_type", AggregateType),
        UniqueConstraint("aggregate_id", "sequence_number", name="aggregate_sequence"),
        Index("ix_events_aggregate", "aggregate_type", "aggregate_id", "sequence_number"),
        Index("ix_events_occurred", "occurred_at"),
    )


class AnalysisJob(Base):
    """Work queue between the Flask app and the independent agent process.

    This table is the process boundary. The app enqueues; the agent polls. The
    agent never imports the Flask application.
    """

    __tablename__ = "analysis_jobs"

    analysis_job_id: Mapped[str] = mapped_column(
        String(UUID_LENGTH), primary_key=True, default=new_identifier
    )
    job_type: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)

    adopter_profile_id: Mapped[str | None] = mapped_column(String(UUID_LENGTH), nullable=True)
    animal_id: Mapped[str | None] = mapped_column(String(UUID_LENGTH), nullable=True)
    application_id: Mapped[str | None] = mapped_column(String(UUID_LENGTH), nullable=True)
    natural_language_query: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        _enum_check("job_type", AnalysisJobType),
        _enum_check("status", AnalysisJobStatus),
        Index("ix_jobs_status_created", "status", "created_at"),
    )
