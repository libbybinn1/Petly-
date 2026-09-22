"""Domain enumerations shared across the system.

Every fixed set of values is an enum rather than a bare string, so an invalid
value is a type error rather than a silent data bug. Values are stored in the
database exactly as written here and are mirrored by CHECK constraints.
"""

from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    """The two roles the system recognises (course blueprint section 3)."""

    ADOPTER = "ADOPTER"
    STAFF = "STAFF"


class HomeType(StrEnum):
    """The adopter's living environment, used for space-compatibility scoring."""

    APARTMENT = "APARTMENT"
    HOUSE = "HOUSE"
    FARM = "FARM"


class ExperienceLevel(StrEnum):
    """How much prior animal-care experience the adopter has."""

    NONE = "NONE"
    SOME = "SOME"
    EXPERIENCED = "EXPERIENCED"


class ActivityLevel(StrEnum):
    """Daily activity, used on both adopters and animals.

    Shared deliberately: matching compares the adopter's activity level against
    the animal's requirement, so both sides must speak the same vocabulary.
    """

    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"


class Species(StrEnum):
    """Animal species the organization handles."""

    DOG = "DOG"
    CAT = "CAT"
    RABBIT = "RABBIT"
    HAMSTER = "HAMSTER"
    GUINEA_PIG = "GUINEA_PIG"
    BIRD = "BIRD"
    OTHER = "OTHER"


class AnimalSize(StrEnum):
    """Physical size band, used for space-requirement matching."""

    SMALL = "SMALL"
    MEDIUM = "MEDIUM"
    LARGE = "LARGE"


class Temperament(StrEnum):
    """The animal's general disposition."""

    CALM = "CALM"
    BALANCED = "BALANCED"
    ENERGETIC = "ENERGETIC"
    ANXIOUS = "ANXIOUS"


class AnimalStatus(StrEnum):
    """Where an animal sits in the adoption pipeline (spec section 25)."""

    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    ADOPTION_IN_PROGRESS = "ADOPTION_IN_PROGRESS"
    ADOPTED = "ADOPTED"
    UNAVAILABLE = "UNAVAILABLE"

    @property
    def is_open_for_applications(self) -> bool:
        """Whether an adopter may apply for an animal in this status."""
        return self is AnimalStatus.AVAILABLE


class ApplicationStatus(StrEnum):
    """Adoption application lifecycle (spec section 25)."""

    SUBMITTED = "SUBMITTED"
    UNDER_REVIEW = "UNDER_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"
    CLOSED = "CLOSED"

    @property
    def is_active(self) -> bool:
        """Whether this application still occupies the adopter.

        Active applications are the ones closed by the cascade when another
        application is approved (spec section 7.5).
        """
        return self in (ApplicationStatus.SUBMITTED, ApplicationStatus.UNDER_REVIEW)

    @property
    def is_final(self) -> bool:
        """Whether this status ends the application's lifecycle."""
        return not self.is_active


class InvitationStatus(StrEnum):
    """Invitation lifecycle (spec section 25).

    Deliberately separate from ApplicationStatus: an accepted invitation starts
    an application, it does not approve an adoption (spec section 7.4).
    """

    SENT = "SENT"
    VIEWED = "VIEWED"
    ACCEPTED = "ACCEPTED"
    DECLINED = "DECLINED"
    EXPIRED = "EXPIRED"

    @property
    def is_awaiting_response(self) -> bool:
        """Whether the adopter can still act on this invitation."""
        return self in (InvitationStatus.SENT, InvitationStatus.VIEWED)


class MatchDirection(StrEnum):
    """Which way a match was computed.

    The two directions use different weightings on purpose (spec section 9):
    adopter-to-animal emphasises the person's lifestyle and preferences, while
    animal-to-adopter emphasises the animal's care needs and temperament.
    """

    ADOPTER_TO_ANIMAL = "ADOPTER_TO_ANIMAL"
    ANIMAL_TO_ADOPTER = "ANIMAL_TO_ADOPTER"


class AnalysisJobType(StrEnum):
    """The kinds of work the independent agent process performs."""

    RANK_APPLICANT = "RANK_APPLICANT"
    FIND_MY_PET = "FIND_MY_PET"
    FIND_MORE_ADOPTERS = "FIND_MORE_ADOPTERS"
    INTERPRET_INTENT = "INTERPRET_INTENT"


class AnalysisJobStatus(StrEnum):
    """Progress of a queued agent job."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class NotificationType(StrEnum):
    """Categories of internal inbox message (spec section 23)."""

    INVITATION_RECEIVED = "INVITATION_RECEIVED"
    INVITATION_RESPONSE = "INVITATION_RESPONSE"
    APPLICATION_STATUS_CHANGED = "APPLICATION_STATUS_CHANGED"
    ANALYSIS_READY = "ANALYSIS_READY"


class DomainEventType(StrEnum):
    """Every event appended to the event store (architecture section 4).

    This enum is the authoritative catalogue. Adding a state transition means
    adding an event here first, which keeps the log and the code in step.
    """

    APPLICATION_SUBMITTED = "ApplicationSubmitted"
    APPLICATION_UNDER_REVIEW = "ApplicationUnderReview"
    APPLICATION_APPROVED = "ApplicationApproved"
    APPLICATION_REJECTED = "ApplicationRejected"
    APPLICATION_WITHDRAWN = "ApplicationWithdrawn"
    APPLICATION_CLOSED_DUE_TO_OTHER_APPROVAL = "ApplicationClosedDueToOtherApproval"
    APPLICATION_REOPENED = "ApplicationReopened"

    INVITATION_SENT = "InvitationSent"
    INVITATION_VIEWED = "InvitationViewed"
    INVITATION_ACCEPTED = "InvitationAccepted"
    INVITATION_DECLINED = "InvitationDeclined"
    INVITATION_EXPIRED = "InvitationExpired"

    AI_ANALYSIS_COMPLETED = "AIAnalysisCompleted"
    ANIMAL_STATUS_CHANGED = "AnimalStatusChanged"
    ADOPTER_PROFILE_UPDATED = "AdopterProfileUpdated"


class AggregateType(StrEnum):
    """The aggregates the event store tracks."""

    APPLICATION = "Application"
    INVITATION = "Invitation"
    ANIMAL = "Animal"
    ADOPTER_PROFILE = "AdopterProfile"
