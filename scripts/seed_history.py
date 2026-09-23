"""Seed the demo records that have a past: applications and invitations.

Split from `scripts/seed.py`, which creates the records that simply exist -
accounts, profiles, animals and their photographs. Everything here goes
through the event store, so each row it writes arrives with the history that
explains it.

Three properties are deliberate:

- **History is appended, not implied.** Every state change writes the same
  event the running application would write, with the same payload and the
  same actor, so `domain_events` is a coherent log rather than a table of
  rows with no cause (architecture section 4).
- **The approval cascade is exercised, not faked.** One adopter's application
  is approved and their other active applications are closed with the causing
  application recorded, exactly as spec section 7.5 requires, so the reopen
  rule has something real to reverse.
- **Seeded match analyses contain no invented prose.** Their reasons and
  concerns are the deterministic scorer's own criterion explanations, and the
  model is recorded as `deterministic-fallback`, because rule R4 forbids
  presenting text as a model's reasoning when no model produced it.
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.domain.enums import (
    ActivityLevel,
    AggregateType,
    AnimalSize,
    AnimalStatus,
    ApplicationStatus,
    DomainEventType,
    ExperienceLevel,
    HomeType,
    InvitationStatus,
    MatchDirection,
    NotificationType,
    Species,
    Temperament,
)
from app.domain.matching import (
    AdopterFacts,
    AnimalFacts,
    MatchScore,
    calculate_match_score,
)
from app.eventstore.store import EventStore
from app.infrastructure.models import (
    AdopterProfile,
    AdoptionApplication,
    AdoptionInvitation,
    Animal,
    MatchAnalysis,
    Notification,
    User,
    new_identifier,
)
from sqlalchemy.orm import Session

# The demo adopter gets a deliberately busy history: several applications, one
# of which is approved, which closes the rest through the spec 7.5 cascade.
APPLICATIONS_FOR_THE_APPROVAL_STORY = 4

# How many applications the other adopters submit. Weighted rather than
# uniform so a few animals attract a crowd and many attract nobody, which is
# what the dashboard's "no applicants" figure exists to surface.
APPLICATION_COUNT_CHOICES = (0, 1, 1, 1, 2, 2, 2, 3, 3, 3, 4)

APPLICATION_STATUS_CHOICES = (
    [ApplicationStatus.SUBMITTED] * 6
    + [ApplicationStatus.UNDER_REVIEW] * 4
    + [ApplicationStatus.REJECTED]
    + [ApplicationStatus.WITHDRAWN]
)

# Oldest and newest an application may be. The upper end is past the
# dashboard's seven-day staleness cutoff on purpose.
OLDEST_APPLICATION_DAYS = 30
NEWEST_APPLICATION_DAYS = 1

# How long ago an animal was moved out of AVAILABLE.
OLDEST_STATUS_CHANGE_DAYS = 45
NEWEST_STATUS_CHANGE_DAYS = 2

# Every invitation state, including the two the previous seed never produced.
# The dashboard reports expired invitations, and a figure that is always zero
# cannot be trusted by whoever reads it.
INVITATION_PLAN: tuple[tuple[InvitationStatus, int], ...] = (
    (InvitationStatus.SENT, 7),
    (InvitationStatus.VIEWED, 6),
    (InvitationStatus.ACCEPTED, 5),
    (InvitationStatus.DECLINED, 5),
    (InvitationStatus.EXPIRED, 9),
)

INVITATION_EXPIRY_HOURS = 72
# Ages for invitations that have expired, and for those still inside the
# window. Kept apart so the status and the clock never contradict each other.
EXPIRED_INVITATION_AGES_HOURS = (80, 96, 140, 200, 260)
OPEN_INVITATION_AGES_HOURS = (1, 4, 10, 24, 40, 60)

# The model name the agent records when the deterministic scorer produced a
# result without a language model. The read side keys "was this written by a
# model?" off this exact value.
DETERMINISTIC_MODEL_NAME = "deterministic-fallback"

# One analysis in this many applications, plus every poor match. Poor matches
# are always analysed because they are what the dashboard's "no suitable
# applicant" figure is computed from.
ANALYSIS_SAMPLE_RATE = 3
POOR_MATCH_SCORE = 50
STRONG_CRITERION_SCORE = 80
WEAK_CRITERION_SCORE = 60
MAXIMUM_REASONS = 3

YOUNG_CHILD_AGE = 12


@dataclass
class SeedContext:
    """Collaborators every seeding helper needs.

    Bundled into one object because passing the session, event store,
    randomizer, clock and staff list separately to each helper made their
    signatures long enough to obscure the arguments that actually vary.
    """

    session: Session
    event_store: EventStore
    randomizer: random.Random
    now: datetime
    staff_users: list[User] = field(default_factory=list)

    def some_staff_member(self) -> User:
        """Pick a staff member to act, so events name a person.

        Every seeded state change was caused by somebody, and the activity
        feed distinguishes an action a person took from one time caused - an
        invitation that expired reads very differently from one a person
        declined. Leaving the actor unset would make the whole feed read as
        "The system".
        """
        return self.randomizer.choice(self.staff_users)


@dataclass(frozen=True)
class SeededApplication:
    """One persisted application, with what the later steps need to know."""

    application_id: str
    profile: AdopterProfile
    animal: Animal
    status: ApplicationStatus
    submitted_at: datetime


@dataclass
class HistoryCounts:
    """What the history pass wrote, for the closing summary."""

    applications: int = 0
    closed_by_cascade: int = 0
    invitations: int = 0
    analyses: int = 0
    status_changes: int = 0


def write_history(
    context: SeedContext, profiles: list[AdopterProfile], animals: list[Animal]
) -> HistoryCounts:
    """Write every demo record that has a recorded past.

    Ordered as the events happened: animals were moved along the pipeline,
    people applied, one adoption was approved, the agent analysed some
    applicants, and staff sent proactive invitations.

    Args:
        context: The open session, event store, randomizer, clock and staff.
        profiles: Every adopter profile, including the incomplete ones.
        animals: Every animal, already flushed so foreign keys resolve.

    Returns:
        Counts of what was written.
    """
    counts = HistoryCounts()

    counts.status_changes = _seed_animal_status_history(context, animals)
    applications = _seed_applications(context, profiles, animals)
    context.session.flush()

    counts.applications = len(applications)
    counts.closed_by_cascade = _seed_approved_adoption_story(
        context, applications, _story_adopter(profiles)
    )
    counts.analyses = _seed_match_analyses(context, applications)
    counts.invitations = _seed_invitations(context, profiles, animals)
    return counts


def _story_adopter(profiles: list[AdopterProfile]) -> AdopterProfile | None:
    """The adopter whose history the demo is narrated through.

    The first completed profile, which is the account README.md tells a
    reviewer to sign in with. Their history is the one somebody will actually
    open, so it is the one given several applications, an approval, and the
    spec 7.5 cascade that closes the rest.

    Args:
        profiles: Every seeded profile, in roster order.

    Returns:
        The profile, or None if somehow none is complete.
    """
    return next((profile for profile in profiles if profile.is_complete), None)


def _seed_animal_status_history(context: SeedContext, animals: list[Animal]) -> int:
    """Record how each animal came to be in a status other than AVAILABLE.

    Every animal arrives available and is moved on by a staff member, so an
    animal that is reserved or adopted has a history. Without these events the
    history view (blueprint section 10) is empty for exactly the animals whose
    story is most worth showing.

    Returns:
        How many status changes were recorded.
    """
    recorded = 0
    for animal in animals:
        if animal.status == AnimalStatus.AVAILABLE.value:
            continue

        age_in_days = context.randomizer.randint(
            NEWEST_STATUS_CHANGE_DAYS, OLDEST_STATUS_CHANGE_DAYS
        )
        changed_at = context.now - timedelta(days=age_in_days)
        context.event_store.append(
            DomainEventType.ANIMAL_STATUS_CHANGED,
            AggregateType.ANIMAL,
            animal.animal_id,
            payload={"from": AnimalStatus.AVAILABLE.value, "to": animal.status},
            actor_user_id=context.some_staff_member().user_id,
            occurred_at=changed_at.replace(tzinfo=UTC),
        )
        recorded += 1
    return recorded


# --------------------------------------------------------------------------
# Applications
# --------------------------------------------------------------------------


def _seed_applications(
    context: SeedContext, profiles: list[AdopterProfile], animals: list[Animal]
) -> list[SeededApplication]:
    """Create applications, appending the matching events to the log.

    Only completed profiles apply: an adopter who never finished registering
    has nothing to submit, and the incomplete profiles exist precisely so the
    eligibility filter has something to exclude (spec section 10).
    """
    available_animals = [
        animal for animal in animals if animal.status == AnimalStatus.AVAILABLE.value
    ]
    applying_profiles = [profile for profile in profiles if profile.is_complete]
    story_adopter = _story_adopter(profiles)
    seeded: list[SeededApplication] = []

    for profile in applying_profiles:
        is_the_story_adopter = profile is story_adopter
        desired_count = (
            APPLICATIONS_FOR_THE_APPROVAL_STORY
            if is_the_story_adopter
            else context.randomizer.choice(APPLICATION_COUNT_CHOICES)
        )
        # Sampled without replacement: two active applications from the same
        # adopter for the same animal violate the uq_active_application index.
        chosen = context.randomizer.sample(
            available_animals, min(desired_count, len(available_animals))
        )
        for animal in chosen:
            status = (
                ApplicationStatus.SUBMITTED
                if is_the_story_adopter
                else context.randomizer.choice(APPLICATION_STATUS_CHOICES)
            )
            seeded.append(_add_application(context, profile, animal, status))

    seeded.extend(_seed_unwinnable_application(context, applying_profiles, seeded, animals))
    return seeded


def _add_application(
    context: SeedContext,
    profile: AdopterProfile,
    animal: Animal,
    status: ApplicationStatus,
) -> SeededApplication:
    """Persist one application and append its lifecycle events."""
    age_in_days = context.randomizer.randint(NEWEST_APPLICATION_DAYS, OLDEST_APPLICATION_DAYS)
    submitted_at = context.now - timedelta(days=age_in_days)
    application_id = new_identifier()

    context.session.add(
        AdoptionApplication(
            application_id=application_id,
            adopter_profile_id=profile.adopter_profile_id,
            animal_id=animal.animal_id,
            status=status.value,
            applicant_message=(
                f"I would love to meet {animal.name}. I think we would suit each other."
            ),
            submitted_at=submitted_at,
            decided_at=(submitted_at + timedelta(days=2)) if status.is_final else None,
        )
    )
    context.event_store.append(
        DomainEventType.APPLICATION_SUBMITTED,
        AggregateType.APPLICATION,
        application_id,
        payload={"animal_id": animal.animal_id, "animal_name": animal.name},
        actor_user_id=profile.user_id,
        occurred_at=submitted_at.replace(tzinfo=UTC),
    )

    # A withdrawal is the adopter's own act; a review or a rejection is the
    # organization's, and the history view distinguishes the two.
    actor_user_id = (
        profile.user_id
        if status is ApplicationStatus.WITHDRAWN
        else context.some_staff_member().user_id
    )
    _append_application_outcome_event(
        context, application_id, animal, status, submitted_at, actor_user_id
    )
    _notify_of_application_outcome(context, profile, animal, status, submitted_at)

    return SeededApplication(
        application_id=application_id,
        profile=profile,
        animal=animal,
        status=status,
        submitted_at=submitted_at,
    )


OUTCOME_EVENTS: dict[ApplicationStatus, DomainEventType] = {
    ApplicationStatus.UNDER_REVIEW: DomainEventType.APPLICATION_UNDER_REVIEW,
    ApplicationStatus.REJECTED: DomainEventType.APPLICATION_REJECTED,
    ApplicationStatus.WITHDRAWN: DomainEventType.APPLICATION_WITHDRAWN,
}


def _append_application_outcome_event(
    context: SeedContext,
    application_id: str,
    animal: Animal,
    status: ApplicationStatus,
    submitted_at: datetime,
    actor_user_id: str,
) -> None:
    """Append the event that moved an application out of SUBMITTED."""
    event_type = OUTCOME_EVENTS.get(status)
    if event_type is None:
        return

    context.event_store.append(
        event_type,
        AggregateType.APPLICATION,
        application_id,
        payload={"animal_id": animal.animal_id},
        actor_user_id=actor_user_id,
        occurred_at=(submitted_at + timedelta(days=1)).replace(tzinfo=UTC),
    )


def _notify_of_application_outcome(
    context: SeedContext,
    profile: AdopterProfile,
    animal: Animal,
    status: ApplicationStatus,
    submitted_at: datetime,
) -> None:
    """Tell the adopter their application was decided (spec section 23).

    Only decisions are announced. A submission needs no notification - the
    adopter is the one who just made it.
    """
    if status is not ApplicationStatus.REJECTED:
        return

    _notify(
        context,
        profile.user_id,
        NotificationType.APPLICATION_STATUS_CHANGED,
        f"Your application for {animal.name} was not successful",
        (
            f"Another home was chosen for {animal.name}. We would be glad to "
            "suggest animals with a similar temperament."
        ),
        submitted_at + timedelta(days=1),
    )


def _seed_unwinnable_application(
    context: SeedContext,
    profiles: list[AdopterProfile],
    already_seeded: list[SeededApplication],
    animals: list[Animal],
) -> list[SeededApplication]:
    """Add one application that cannot succeed, on purpose.

    The dashboard reports available animals whose applicants all score below
    the recommendation threshold (spec section 22). That figure is computed
    from stored analyses, so the demo needs at least one animal whose only
    applicant is a genuine mismatch - here, the household with the least free
    time applying for an animal that needs a large home it does not have.

    Returns:
        A single-item list, or an empty one when the roster offers no suitable
        pairing.
    """
    spoken_for = {seeded.animal.animal_id for seeded in already_seeded}
    animal = next(
        (
            candidate
            for candidate in animals
            if candidate.status == AnimalStatus.AVAILABLE.value
            and candidate.required_space == AnimalSize.LARGE.value
            and candidate.animal_id not in spoken_for
        ),
        None,
    )
    cramped_profiles = [
        profile
        for profile in profiles
        if profile.home_type == HomeType.APARTMENT.value and not profile.has_yard
    ]
    if animal is None or not cramped_profiles:
        return []

    profile = min(cramped_profiles, key=lambda candidate: float(candidate.daily_hours_available))
    return [_add_application(context, profile, animal, ApplicationStatus.SUBMITTED)]


def _seed_approved_adoption_story(
    context: SeedContext,
    seeded: list[SeededApplication],
    story_adopter: AdopterProfile | None,
) -> int:
    """Approve one application and close the adopter's others (spec 7.5).

    Written through the same events the real command emits, including the
    `caused_by_application_id` payload, because that is what makes a later
    reversal reopen exactly the applications this approval closed rather than
    every application the adopter ever had.

    Two things are pinned rather than random, so the demo tells the same story
    every time: the first staff member decides, and the story adopter is
    preferred - a cascade on an account nobody signs into is a cascade nobody
    sees.

    Returns:
        How many other applications the cascade closed.
    """
    by_profile: dict[str, list[SeededApplication]] = defaultdict(list)
    for application in seeded:
        if application.status.is_active:
            by_profile[application.profile.adopter_profile_id].append(application)

    story_profile_id = story_adopter.adopter_profile_id if story_adopter else ""
    candidates = sorted(
        (group for group in by_profile.values() if len(group) > 1),
        key=lambda group: (
            group[0].profile.adopter_profile_id != story_profile_id,
            -len(group),
            group[0].application_id,
        ),
    )
    if not candidates:
        return 0

    deciding_staff = context.staff_users[0]
    approved, *others = candidates[0]
    _approve_application(context, approved, deciding_staff)
    for other in others:
        _close_application_because_of(context, other, approved, deciding_staff)
    return len(others)


def _approve_application(
    context: SeedContext, application: SeededApplication, deciding_staff: User
) -> None:
    """Mark one application approved and move its animal along the pipeline."""
    decided_at = application.submitted_at + timedelta(days=3)

    row = context.session.get(AdoptionApplication, application.application_id)
    if row is None:
        return
    row.status = ApplicationStatus.APPROVED.value
    row.decided_at = decided_at
    row.decided_by_user_id = deciding_staff.user_id

    context.event_store.append(
        DomainEventType.APPLICATION_APPROVED,
        AggregateType.APPLICATION,
        application.application_id,
        payload={"animal_id": application.animal.animal_id},
        actor_user_id=deciding_staff.user_id,
        occurred_at=decided_at.replace(tzinfo=UTC),
    )
    _move_animal_into_adoption(context, application, deciding_staff, decided_at)
    _notify(
        context,
        application.profile.user_id,
        NotificationType.APPLICATION_STATUS_CHANGED,
        "Your application was approved",
        f"Congratulations. A staff member will be in touch about {application.animal.name}.",
        decided_at,
    )


def _move_animal_into_adoption(
    context: SeedContext,
    application: SeededApplication,
    deciding_staff: User,
    decided_at: datetime,
) -> None:
    """Move an approved application's animal to ADOPTION_IN_PROGRESS."""
    previous_status = application.animal.status
    application.animal.status = AnimalStatus.ADOPTION_IN_PROGRESS.value
    application.animal.updated_at = decided_at

    context.event_store.append(
        DomainEventType.ANIMAL_STATUS_CHANGED,
        AggregateType.ANIMAL,
        application.animal.animal_id,
        payload={"from": previous_status, "to": AnimalStatus.ADOPTION_IN_PROGRESS.value},
        actor_user_id=deciding_staff.user_id,
        occurred_at=decided_at.replace(tzinfo=UTC),
    )


def _close_application_because_of(
    context: SeedContext,
    application: SeededApplication,
    approved: SeededApplication,
    deciding_staff: User,
) -> None:
    """Close one application because another of the adopter's was approved."""
    decided_at = approved.submitted_at + timedelta(days=3)

    row = context.session.get(AdoptionApplication, application.application_id)
    if row is None:
        return
    row.status = ApplicationStatus.CLOSED.value
    row.closed_because_application_id = approved.application_id
    row.decided_at = decided_at

    context.event_store.append(
        DomainEventType.APPLICATION_CLOSED_DUE_TO_OTHER_APPROVAL,
        AggregateType.APPLICATION,
        application.application_id,
        payload={
            "caused_by_application_id": approved.application_id,
            "animal_id": application.animal.animal_id,
        },
        actor_user_id=deciding_staff.user_id,
        occurred_at=decided_at.replace(tzinfo=UTC),
    )


# --------------------------------------------------------------------------
# Stored match analyses
# --------------------------------------------------------------------------


def _seed_match_analyses(context: SeedContext, seeded: list[SeededApplication]) -> int:
    """Store a deterministic analysis for a sample of the applications.

    The agent normally writes these. Seeding some means the staff screens and
    the dashboard have content before the agent has ever run, and the
    "available animals with no suitable applicant" figure is computed from
    stored analyses, so it would otherwise always read zero.

    Every poor match is analysed and roughly one in three of the rest, so the
    demo shows both the reassuring and the actionable case.

    Returns:
        How many analyses were written.
    """
    written = 0
    for index, application in enumerate(seeded):
        score = calculate_match_score(
            _adopter_facts(application.profile),
            _animal_facts(application.animal),
            MatchDirection.ANIMAL_TO_ADOPTER,
        )
        is_poor_match = score.is_disqualified or score.score < POOR_MATCH_SCORE
        if not is_poor_match and index % ANALYSIS_SAMPLE_RATE:
            continue

        _add_match_analysis(context, application, score)
        written += 1
    return written


def _add_match_analysis(
    context: SeedContext, application: SeededApplication, score: MatchScore
) -> None:
    """Persist one analysis and append its event."""
    reasons, concerns = _analysis_prose(score)
    generated_at = application.submitted_at + timedelta(hours=1)

    context.session.add(
        MatchAnalysis(
            match_analysis_id=new_identifier(),
            direction=MatchDirection.ANIMAL_TO_ADOPTER.value,
            adopter_profile_id=application.profile.adopter_profile_id,
            animal_id=application.animal.animal_id,
            application_id=application.application_id,
            score=score.score,
            is_disqualified=score.is_disqualified,
            # NVARCHAR(MAX) columns: SQL Server 2014 has no JSON type.
            criterion_scores=json.dumps(_criterion_rows(score)),
            reasons=json.dumps(reasons),
            concerns=json.dumps(concerns),
            missing_information=json.dumps([]),
            evidence_sources=json.dumps([]),
            used_web_search=False,
            model_name=DETERMINISTIC_MODEL_NAME,
            generated_at=generated_at,
        )
    )
    context.event_store.append(
        DomainEventType.AI_ANALYSIS_COMPLETED,
        AggregateType.APPLICATION,
        application.application_id,
        payload={"animal_id": application.animal.animal_id, "score": score.score},
        occurred_at=generated_at.replace(tzinfo=UTC),
    )


def _criterion_rows(score: MatchScore) -> list[dict[str, object]]:
    """Shape a score's breakdown the way the read side expects it."""
    return [
        {
            "criterion": criterion.criterion.value,
            "score": criterion.score,
            "weight": criterion.weight,
            "explanation": criterion.explanation,
        }
        for criterion in score.criterion_scores
    ]


def _analysis_prose(score: MatchScore) -> tuple[list[str], list[str]]:
    """Split a score's criterion explanations into reasons and concerns.

    No sentence is written here that the deterministic scorer did not produce.
    Rule R4 forbids presenting invented prose as an assessment, and these rows
    land in the same table the agent writes to.
    """
    if score.is_disqualified:
        return [], [score.disqualification_reason or "This pairing is not permissible."]

    strongest_first = sorted(score.criterion_scores, key=lambda item: -item.score)
    reasons = [
        criterion.explanation
        for criterion in strongest_first[:MAXIMUM_REASONS]
        if criterion.score >= STRONG_CRITERION_SCORE
    ]
    concerns = [
        criterion.explanation
        for criterion in strongest_first
        if criterion.score < WEAK_CRITERION_SCORE
    ]
    return reasons, concerns


def _adopter_facts(profile: AdopterProfile) -> AdopterFacts:
    """Convert a seeded profile into the domain value object.

    The read side has its own converter, but reaching into a query module for
    a private helper would couple this script to that module's internals. The
    stored values came from typed enums moments ago, so this cannot fail.
    """
    return AdopterFacts(
        home_type=HomeType(profile.home_type),
        has_yard=bool(profile.has_yard),
        household_has_children=bool(profile.household_has_children),
        youngest_child_age=profile.youngest_child_age,
        has_other_animals=bool(profile.has_other_animals),
        experience_level=ExperienceLevel(profile.experience_level),
        activity_level=ActivityLevel(profile.activity_level),
        daily_hours_available=float(profile.daily_hours_available),
        city=profile.city,
        preferred_species=frozenset(
            Species(value) for value in (profile.preferred_species or "").split(",") if value
        ),
        open_to_proactive_suggestions=bool(profile.open_to_proactive_suggestions),
        is_complete=bool(profile.is_complete),
    )


def _animal_facts(animal: Animal) -> AnimalFacts:
    """Convert a seeded animal into the domain value object."""
    return AnimalFacts(
        species=Species(animal.species),
        age_years=float(animal.age_years),
        size=AnimalSize(animal.size),
        temperament=Temperament(animal.temperament),
        activity_level=ActivityLevel(animal.activity_level),
        good_with_children=bool(animal.good_with_children),
        good_with_other_animals=bool(animal.good_with_other_animals),
        has_special_needs=bool(animal.has_special_needs),
        required_space=AnimalSize(animal.required_space),
        city=animal.city,
    )


# --------------------------------------------------------------------------
# Invitations
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class InvitationTimeline:
    """When an invitation was sent, seen, answered, and when it expires."""

    sent_at: datetime
    expires_at: datetime
    viewed_at: datetime | None
    responded_at: datetime | None


@dataclass(frozen=True)
class InvitationActors:
    """Who did what to one invitation, and when.

    Bundled because the event stream needs both parties and the timeline, and
    a five-argument event helper reads as a list of anonymous strings.
    """

    status: InvitationStatus
    timeline: InvitationTimeline
    sent_by_user_id: str
    responded_by_user_id: str


def _seed_invitations(
    context: SeedContext, profiles: list[AdopterProfile], animals: list[Animal]
) -> int:
    """Create invitations covering every state in the lifecycle (spec 5.4).

    Only adopters with a completed profile who opted in receive one, because
    that is the eligibility rule proactive discovery applies (spec section 10),
    and seeded data that broke it would make the rule look optional.

    Returns:
        How many invitations were created.
    """
    eligible = [
        profile
        for profile in profiles
        if profile.open_to_proactive_suggestions and profile.is_complete
    ]
    available = [animal for animal in animals if animal.status == AnimalStatus.AVAILABLE.value]
    if not eligible or not available:
        return 0

    used_pairs: set[tuple[str, str]] = set()
    created = 0
    for status, count in INVITATION_PLAN:
        for _ in range(count):
            pair = _choose_unused_pair(context, eligible, available, used_pairs)
            if pair is None:
                continue
            profile, animal = pair
            _add_invitation(context, profile, animal, status, context.some_staff_member())
            created += 1
    return created


def _choose_unused_pair(
    context: SeedContext,
    profiles: list[AdopterProfile],
    animals: list[Animal],
    used_pairs: set[tuple[str, str]],
) -> tuple[AdopterProfile, Animal] | None:
    """Pick an adopter and animal who have not already been paired.

    Inviting the same adopter to meet the same animal twice would read as a
    bug on the invitations page, so pairs are drawn without replacement.
    """
    for _ in range(len(profiles) * len(animals)):
        profile = context.randomizer.choice(profiles)
        animal = context.randomizer.choice(animals)
        key = (profile.adopter_profile_id, animal.animal_id)
        if key in used_pairs:
            continue
        used_pairs.add(key)
        return profile, animal
    return None


def _add_invitation(
    context: SeedContext,
    profile: AdopterProfile,
    animal: Animal,
    status: InvitationStatus,
    sending_staff: User,
) -> None:
    """Persist one invitation with its events and notifications."""
    timeline = _invitation_timeline(context, status)
    invitation_id = new_identifier()

    context.session.add(
        AdoptionInvitation(
            invitation_id=invitation_id,
            animal_id=animal.animal_id,
            adopter_profile_id=profile.adopter_profile_id,
            sent_by_user_id=sending_staff.user_id,
            status=status.value,
            staff_message=(
                f"We thought {animal.name} might be a good fit for your home. "
                "Would you like to meet?"
            ),
            sent_at=timeline.sent_at,
            expires_at=timeline.expires_at,
            viewed_at=timeline.viewed_at,
            responded_at=timeline.responded_at,
        )
    )
    _append_invitation_events(
        context,
        invitation_id,
        animal,
        InvitationActors(
            status=status,
            timeline=timeline,
            sent_by_user_id=sending_staff.user_id,
            responded_by_user_id=profile.user_id,
        ),
    )
    _notify(
        context,
        profile.user_id,
        NotificationType.INVITATION_RECEIVED,
        f"Invitation to meet {animal.name}",
        f"{sending_staff.full_name} thinks {animal.name} could suit your home.",
        timeline.sent_at,
    )
    _notify_staff_of_response(context, sending_staff, animal, status, timeline)


def _invitation_timeline(context: SeedContext, status: InvitationStatus) -> InvitationTimeline:
    """Build timestamps that agree with the invitation's status.

    An expired invitation must be older than the 72-hour window and an open
    one must be inside it, or the dashboard's expiry figure and the row it
    counted would contradict each other.
    """
    ages = (
        EXPIRED_INVITATION_AGES_HOURS
        if status is InvitationStatus.EXPIRED
        else OPEN_INVITATION_AGES_HOURS
    )
    sent_at = context.now - timedelta(hours=context.randomizer.choice(ages))
    was_seen = status not in (InvitationStatus.SENT, InvitationStatus.EXPIRED)
    was_answered = status in (InvitationStatus.ACCEPTED, InvitationStatus.DECLINED)

    return InvitationTimeline(
        sent_at=sent_at,
        expires_at=sent_at + timedelta(hours=INVITATION_EXPIRY_HOURS),
        viewed_at=sent_at + timedelta(hours=1) if was_seen else None,
        responded_at=sent_at + timedelta(hours=2) if was_answered else None,
    )


RESPONSE_EVENTS: dict[InvitationStatus, DomainEventType] = {
    InvitationStatus.ACCEPTED: DomainEventType.INVITATION_ACCEPTED,
    InvitationStatus.DECLINED: DomainEventType.INVITATION_DECLINED,
}


def _append_invitation_events(
    context: SeedContext,
    invitation_id: str,
    animal: Animal,
    actors: InvitationActors,
) -> None:
    """Append the full event stream for one invitation.

    Expiry is the one event with no actor: nobody expired the invitation, the
    clock did, and the history view marks such entries as caused by the system
    rather than by a person.
    """
    timeline = actors.timeline
    context.event_store.append(
        DomainEventType.INVITATION_SENT,
        AggregateType.INVITATION,
        invitation_id,
        payload={"animal_id": animal.animal_id, "animal_name": animal.name},
        actor_user_id=actors.sent_by_user_id,
        occurred_at=timeline.sent_at.replace(tzinfo=UTC),
    )
    if timeline.viewed_at is not None:
        context.event_store.append(
            DomainEventType.INVITATION_VIEWED,
            AggregateType.INVITATION,
            invitation_id,
            payload={"animal_id": animal.animal_id},
            actor_user_id=actors.responded_by_user_id,
            occurred_at=timeline.viewed_at.replace(tzinfo=UTC),
        )

    response_event = RESPONSE_EVENTS.get(actors.status)
    if response_event is not None and timeline.responded_at is not None:
        context.event_store.append(
            response_event,
            AggregateType.INVITATION,
            invitation_id,
            payload={"animal_id": animal.animal_id},
            actor_user_id=actors.responded_by_user_id,
            occurred_at=timeline.responded_at.replace(tzinfo=UTC),
        )
    if actors.status is InvitationStatus.EXPIRED:
        context.event_store.append(
            DomainEventType.INVITATION_EXPIRED,
            AggregateType.INVITATION,
            invitation_id,
            payload={"animal_id": animal.animal_id},
            occurred_at=timeline.expires_at.replace(tzinfo=UTC),
        )


def _notify_staff_of_response(
    context: SeedContext,
    sending_staff: User,
    animal: Animal,
    status: InvitationStatus,
    timeline: InvitationTimeline,
) -> None:
    """Tell the sending staff member that an adopter answered (spec 23)."""
    if timeline.responded_at is None:
        return

    verb = "accepted" if status is InvitationStatus.ACCEPTED else "declined"
    _notify(
        context,
        sending_staff.user_id,
        NotificationType.INVITATION_RESPONSE,
        f"An invitation for {animal.name} was {verb}",
        f"The adopter you invited to meet {animal.name} has {verb} the invitation.",
        timeline.responded_at,
    )


def _notify(
    context: SeedContext,
    user_id: str,
    notification_type: NotificationType,
    title: str,
    body: str,
    created_at: datetime,
) -> None:
    """Add one internal inbox message (spec section 23)."""
    is_about_an_invitation = notification_type in (
        NotificationType.INVITATION_RECEIVED,
        NotificationType.INVITATION_RESPONSE,
    )
    context.session.add(
        Notification(
            notification_id=new_identifier(),
            user_id=user_id,
            notification_type=notification_type.value,
            title=title,
            body=body,
            link_url="/my/invitations" if is_about_an_invitation else "/my/applications",
            is_read=context.randomizer.choice([True, False]),
            created_at=created_at,
        )
    )
