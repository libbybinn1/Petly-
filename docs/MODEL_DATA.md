# MODEL_DATA — PetMatch

**Entities, relationships, keys and constraints.**
Target engine: **Microsoft SQL Server 2014 Express (SP3)** on Somee.com.
Sources: blueprint §11; PetMatch spec §5, §18, §25.

The authority for everything below is `app/infrastructure/models.py`. Where a
constraint is enforced in Python rather than by the database, this document
says which, because the difference decides whether two concurrent requests can
both commit.

---

## 0. Engine constraints that shaped this schema

SQL Server 2014 is an old engine. These are not preferences — they are hard
limits that dictated concrete choices below.

| Limitation | Available from | Consequence here |
|---|---|---|
| No native `JSON` type or functions | 2016 | `payload`, `criterion_scores`, `reasons`, `concerns`, `missing_information`, `evidence_sources`, `reasoning_trace` and `result_payload` are `NVARCHAR(MAX)`, serialized in Python. No `JSON_VALUE`, `OPENJSON` or `ISJSON`. |
| No `STRING_AGG` | 2017 | String aggregation happens in Python. `preferred_species` is one comma-separated column, parsed on read. |
| `DATETIME` carries no offset | — | Every timestamp is stored **naive, in UTC**, and read back as UTC. See §0.1. |
| Free-tier size cap | — | Images are files on disk; the database stores only a URL. No BLOBs. |
| `OFFSET ... FETCH NEXT` **is** supported | 2012 | Ordinary offset pagination is fine. |
| Filtered indexes **are** supported | 2008 | The two rules in §2.4 and §2.5 are enforced by the database, not only by a handler. |

**Identifiers are `NVARCHAR(36)`, not `UNIQUEIDENTIFIER`.** SQLAlchemy's mssql
dialect has no UUID column type that round-trips cleanly across drivers, so
`new_identifier()` generates a UUID4 and stores its 36-character string form.
Comparisons stay exact and the indexes remain usable. This also suits event
sourcing: an aggregate's identifier must exist *before* its first event is
appended, so the application cannot wait for a database-generated identity
value.

**There are no SQL `DEFAULT` clauses.** Every default in this document is a
Python-side default on the mapped column, applied by SQLAlchemy on insert.
Nothing writes to these tables except this application and its scripts, so
the distinction costs nothing — but a row inserted by hand in a SQL console
would need every column supplied.

**There is no Alembic.** The schema is created from the SQLAlchemy metadata by
`scripts/db.py` (`create`, `reset`, `fresh`). Constraint names are generated
by a metadata naming convention, because SQL Server requires constraint names
to be unique per *database*, not per table, and four tables carry a `status`
column.

### 0.1 Timestamps

`utc_now()` in `app/infrastructure/` is the one place an aware datetime
becomes the naive UTC value these columns store. `EventStore.append` and every
command handler go through it for `occurred_at`, `submitted_at`, `sent_at`,
`expires_at` and the rest; every read attaches UTC again. Ruff's `DTZ` rules keep values aware everywhere else, so the
conversion at the persistence boundary is the only place naivety can enter —
which matters because the 72-hour invitation window is wrong if a naive value
is ever compared with an aware one.

---

## 1. Entity relationship overview

```
      User (1) ──────── (0..1) AdopterProfile
        │                        │
        │ actor                  │
        │                        ├──< AdoptionApplication >── Animal
        │                        │                              │
        │                        ├──< AdoptionInvitation >──────┤
        │                        │                              │
        │                        └──< MatchAnalysis >───────────┘
        │
        ├──< Notification
        └──  DomainEvent.actor_user_id   (a plain column, not a foreign key)

      Animal (1) ──< AnimalImage        (ON DELETE CASCADE)
      AnalysisJob                        (no foreign keys at all — see §2.10)
```

Declared foreign keys, in full: `adopter_profiles.user_id`,
`animal_images.animal_id`, `adoption_applications.adopter_profile_id`,
`.animal_id` and `.decided_by_user_id`, `adoption_invitations.animal_id`,
`.adopter_profile_id` and `.sent_by_user_id`,
`match_analyses.adopter_profile_id` and `.animal_id`, and
`notifications.user_id`. Everything else that holds an identifier —
`adoption_applications.originating_invitation_id` and
`.closed_because_application_id`, `match_analyses.application_id`,
`domain_events.actor_user_id`, and every identifier on `analysis_jobs` — is a
plain `NVARCHAR(36)` column with no referential constraint.

That is a deliberate choice in two of those cases and an accepted looseness in
the rest. The event log must be able to record an actor whose account is later
removed, and the job queue is a boundary between processes whose rows outlive
the records they refer to; a foreign key would turn housekeeping into a
cascade. `closed_because_application_id` is a self-reference that a filtered
index would have to police anyway, and the event log is its real authority.

---

## 2. Tables

### 2.1 `users`

| Column | Type | Constraints |
|---|---|---|
| `user_id` | NVARCHAR(36) | PK, UUID4 string |
| `email` | NVARCHAR(254) | NOT NULL, UNIQUE |
| `password_hash` | NVARCHAR(255) | NOT NULL |
| `full_name` | NVARCHAR(150) | NOT NULL |
| `role` | NVARCHAR(20) | NOT NULL, CHECK IN ('ADOPTER','STAFF') |
| `is_active` | BIT | NOT NULL, Python default 1 |
| `created_at` | DATETIME | NOT NULL, naive UTC |

> Two roles satisfy blueprint §3. `is_active` participates in the eligibility
> rules of spec §10, and the session loader re-reads it on every request, so
> deactivating an account ends a live session rather than waiting for the next
> sign-in.

The UNIQUE index on `email` is what settles a registration race. The handler
checks first so it can answer with a readable message, but a check-then-insert
is not atomic and two overlapping registrations both pass it.

### 2.2 `adopter_profiles`

One-to-one with a user of role `ADOPTER`.

| Column | Type | Constraints |
|---|---|---|
| `adopter_profile_id` | NVARCHAR(36) | PK |
| `user_id` | NVARCHAR(36) | FK → users, UNIQUE, NOT NULL |
| `home_type` | NVARCHAR(20) | NOT NULL, CHECK IN ('APARTMENT','HOUSE','FARM') |
| `has_yard` | BIT | NOT NULL |
| `yard_size_sqm` | INT | NULL, CHECK IS NULL OR >= 0 |
| `household_has_children` | BIT | NOT NULL |
| `youngest_child_age` | INT | NULL, CHECK IS NULL OR BETWEEN 0 AND 18 |
| `has_other_animals` | BIT | NOT NULL |
| `other_animals_description` | NVARCHAR(500) | NULL |
| `experience_level` | NVARCHAR(20) | NOT NULL, CHECK IN ('NONE','SOME','EXPERIENCED') |
| `activity_level` | NVARCHAR(20) | NOT NULL, CHECK IN ('LOW','MODERATE','HIGH') |
| `daily_hours_available` | DECIMAL(4,1) | NOT NULL, CHECK BETWEEN 0 AND 24 |
| `city` | NVARCHAR(100) | NOT NULL |
| `preferred_species` | NVARCHAR(200) | NULL, comma-separated `Species` values |
| `preferred_size` | NVARCHAR(20) | NULL, an `AnimalSize` value |
| `preferred_age_range` | NVARCHAR(20) | NULL, an `AgePreference` value |
| **`open_to_proactive_suggestions`** | BIT | NOT NULL, Python default 0 |
| `is_complete` | BIT | NOT NULL, Python default 0 |
| `created_at` / `updated_at` | DATETIME | NOT NULL, naive UTC |

> `open_to_proactive_suggestions` is required by spec §5.1 and is a hard
> eligibility gate for Find More Adopters (spec §10). `is_complete` is another.
> Neither is ever inferred: an absent checkbox means "no", not "unspecified".

**`preferred_size` and `preferred_age_range` are the two preference columns
the matching engine reads as its 11th criterion.** Their vocabularies are
closed:

| Column | Permitted values |
|---|---|
| `preferred_size` | `SMALL`, `MEDIUM`, `LARGE` (`AnimalSize`) |
| `preferred_age_range` | `0-2 years`, `2-8 years`, `8+ years`, `any` (`AgePreference`) |

Bands rather than a number, because that is how people think about it:
somebody wants "a puppy" or "an older, calmer one", not an animal between 2.0
and 8.0 years. `any` is a *stated openness*, not a missing answer — an adopter
who picked it has told us something, and scores differently from one who left
it blank.

Note that the search filter's age bands are spelled differently — `0-2`,
`2-8`, `8+` (`AGE_RANGE_BOUNDS` in `app/cqrs/queries/animal_queries.py`) —
because one is a stored preference and the other a URL parameter. They cover
the same three ranges.

`preferred_species` is parsed leniently on read: `"CAT, DOG,,"` yields exactly
those two species, duplicates collapse, and a value the enum does not define
is **dropped** rather than mapped to `OTHER` — a corrupt preference must not
invent a preference for exotic animals.

### 2.3 `animals`

| Column | Type | Constraints |
|---|---|---|
| `animal_id` | NVARCHAR(36) | PK |
| `name` | NVARCHAR(100) | NOT NULL |
| `species` | NVARCHAR(30) | NOT NULL, CHECK IN ('DOG','CAT','RABBIT','HAMSTER','GUINEA_PIG','BIRD','OTHER') |
| `breed` | NVARCHAR(100) | NULL |
| `age_years` | DECIMAL(4,1) | NOT NULL, CHECK >= 0 |
| `size` | NVARCHAR(20) | NOT NULL, CHECK IN ('SMALL','MEDIUM','LARGE') |
| `temperament` | NVARCHAR(30) | NOT NULL, CHECK IN ('CALM','BALANCED','ENERGETIC','ANXIOUS') |
| `activity_level` | NVARCHAR(20) | NOT NULL, CHECK IN ('LOW','MODERATE','HIGH') |
| `good_with_children` | BIT | NOT NULL, Python default 1 |
| `good_with_other_animals` | BIT | NOT NULL, Python default 1 |
| `has_special_needs` | BIT | NOT NULL, Python default 0 |
| `special_needs_description` | NVARCHAR(1000) | NULL |
| `required_space` | NVARCHAR(20) | NOT NULL, CHECK IN ('SMALL','MEDIUM','LARGE') |
| `city` | NVARCHAR(100) | NOT NULL |
| `status` | NVARCHAR(30) | NOT NULL, CHECK IN ('AVAILABLE','RESERVED','ADOPTION_IN_PROGRESS','ADOPTED','UNAVAILABLE') |
| `description` | NVARCHAR(MAX) | NULL |
| `created_at` / `updated_at` | DATETIME | NOT NULL, naive UTC |

Indexes: `ix_animals_status_species` on `(status, species)`, `ix_animals_city`
on `city`.

`updated_at` is not decorative: the agent's analysis cache compares it with a
stored analysis's `generated_at` to decide whether that explanation still
describes the current animal (see §2.7).

### 2.4 `animal_images`

Spec §24 makes images mandatory — every animal must have at least one.

| Column | Type | Constraints |
|---|---|---|
| `animal_image_id` | NVARCHAR(36) | PK |
| `animal_id` | NVARCHAR(36) | FK → animals, **ON DELETE CASCADE** |
| `image_url` | NVARCHAR(500) | NOT NULL |
| `is_primary` | BIT | NOT NULL, Python default 0 |
| `display_order` | INT | NOT NULL, Python default 0 |
| `uploaded_at` | DATETIME | NOT NULL, naive UTC |

Indexes: `ix_animal_images_animal` on `animal_id`, plus a **filtered unique
index** enforcing one primary image per animal:

```sql
CREATE UNIQUE INDEX uq_animal_primary_image
  ON animal_images(animal_id)
  WHERE is_primary = 1;
```

A plain unique index on `animal_id` would allow only one image per animal
altogether, which is the opposite of what a gallery needs. The filter is
declared for both mssql and sqlite, so the constraint holds in tests as well
as in production.

**The "at least one image" rule cannot be a table constraint** — it is a
cross-table cardinality rule, and no schema-level construct expresses it. It
lives in `app/domain/animal_rules.py`:

- `validate_animal` refuses a submission whose `image_urls` are empty or only
  whitespace, so the form reports it like any other field error;
- `ensure_animal_has_an_image` raises `NoImageError` on the **write** side,
  independently of the form, and is called on **both** the create and the edit
  paths — an edit that removed the last photograph would otherwise slip past a
  rule the create path enforced.

### 2.5 `adoption_applications`

| Column | Type | Constraints |
|---|---|---|
| `application_id` | NVARCHAR(36) | PK |
| `adopter_profile_id` | NVARCHAR(36) | FK → adopter_profiles, NOT NULL |
| `animal_id` | NVARCHAR(36) | FK → animals, NOT NULL |
| `status` | NVARCHAR(30) | NOT NULL, CHECK IN ('SUBMITTED','UNDER_REVIEW','APPROVED','REJECTED','WITHDRAWN','CLOSED') |
| `applicant_message` | NVARCHAR(2000) | NULL |
| `originating_invitation_id` | NVARCHAR(36) | NULL, no FK |
| `closed_because_application_id` | NVARCHAR(36) | NULL, no FK (self-reference) |
| `submitted_at` | DATETIME | NOT NULL |
| `decided_at` | DATETIME | NULL |
| `decided_by_user_id` | NVARCHAR(36) | FK → users, NULL |

Indexes: `ix_applications_animal_status` on `(animal_id, status)`,
`ix_applications_adopter` on `adopter_profile_id`, plus a **filtered unique
index** preventing duplicate *active* applications while still allowing a
re-application after a rejection:

```sql
CREATE UNIQUE INDEX uq_active_application
  ON adoption_applications(adopter_profile_id, animal_id)
  WHERE status IN ('SUBMITTED','UNDER_REVIEW');
```

This is the database half of FR-7.2. The handler still checks first, so it can
answer with a readable message instead of an integrity error — but a
check-then-insert is not atomic, two overlapping requests both pass it, and
only the index settles that race.
`tests/integration/test_qa_cascade_and_concurrency.py::test_the_database_itself_forbids_duplicate_active_applications`
proves the index, not the check, is what holds.

`applicant_message` is not write-only: it is shown back to the adopter on
`/my/applications` and to staff on the applicant ranking. Its 2000-character
limit is enforced in the controller, because it is the one free-text field an
adopter controls entirely and therefore the natural place to push an oversized
payload.

> `closed_because_application_id` is a **projection** of the
> `ApplicationClosedDueToOtherApproval` event. It exists for query speed and
> as a cross-check. The event log is the authority: `ReverseApprovalHandler`
> derives what to reopen by reading the closing events and keeping the latest
> cause per application, so an application closed by A, reopened, then closed
> again by B is not resurrected by reversing A a second time. See
> `docs/ARCHITECTURE.md` §4.

### 2.6 `adoption_invitations`

| Column | Type | Constraints |
|---|---|---|
| `invitation_id` | NVARCHAR(36) | PK |
| `animal_id` | NVARCHAR(36) | FK → animals, NOT NULL |
| `adopter_profile_id` | NVARCHAR(36) | FK → adopter_profiles, NOT NULL |
| `sent_by_user_id` | NVARCHAR(36) | FK → users (STAFF), NOT NULL |
| `status` | NVARCHAR(20) | NOT NULL, CHECK IN ('SENT','VIEWED','ACCEPTED','DECLINED','EXPIRED') |
| `staff_message` | NVARCHAR(1000) | NULL |
| `sent_at` | DATETIME | NOT NULL |
| `expires_at` | DATETIME | NOT NULL |
| `viewed_at` / `responded_at` | DATETIME | NULL |

Indexes: `ix_invitations_adopter_status` on `(adopter_profile_id, status)`,
`ix_invitations_expires` on `expires_at` — the column the sweep scans.

`expires_at = sent_at + INVITATION_EXPIRY_HOURS`. The default is **72 hours**
(spec §7.4, `DEFAULT_RESPONSE_WINDOW_HOURS`), and the value is read from
configuration and passed to `SendInvitationHandler` rather than hard-coded, so
a demonstration can shorten the window. `calculate_expiry` refuses a window of
zero — a misconfiguration that expired every invitation instantly would look
like a bug in the feature — and refuses a naive `sent_at`, because a naive
value compared against an aware one is how a 72-hour window silently becomes
a wrong one.

### 2.7 `match_analyses`

| Column | Type | Constraints |
|---|---|---|
| `match_analysis_id` | NVARCHAR(36) | PK |
| `direction` | NVARCHAR(20) | NOT NULL, CHECK IN ('ADOPTER_TO_ANIMAL','ANIMAL_TO_ADOPTER') |
| `adopter_profile_id` | NVARCHAR(36) | FK → adopter_profiles, NOT NULL |
| `animal_id` | NVARCHAR(36) | FK → animals, NOT NULL |
| `application_id` | NVARCHAR(36) | NULL, no FK |
| `score` | INT | NOT NULL, CHECK BETWEEN 0 AND 100 |
| `is_disqualified` | BIT | NOT NULL, Python default 0 |
| `criterion_scores` | NVARCHAR(MAX) | NOT NULL, JSON array, default `[]` |
| `reasons` | NVARCHAR(MAX) | NOT NULL, JSON array, default `[]` |
| `concerns` | NVARCHAR(MAX) | NOT NULL, JSON array, default `[]` |
| `missing_information` | NVARCHAR(MAX) | NOT NULL, JSON array, default `[]` |
| `evidence_sources` | NVARCHAR(MAX) | NOT NULL, JSON array, default `[]` |
| **`reasoning_trace`** | NVARCHAR(MAX) | **NULL**, JSON array |
| `used_web_search` | BIT | NOT NULL, Python default 0 |
| `model_name` | NVARCHAR(100) | NOT NULL |
| `generated_at` | DATETIME | NOT NULL |

Indexes: `ix_analyses_animal_direction` on `(animal_id, direction)`,
`ix_analyses_adopter_direction` on `(adopter_profile_id, direction)`, and two
filtered unique indexes that make the worker's "update in place, never
duplicate" rule a database guarantee rather than a docstring:
`uq_analysis_per_application` on `(adopter_profile_id, animal_id, direction,
application_id) WHERE application_id IS NOT NULL` and
`uq_analysis_without_application` on `(adopter_profile_id, animal_id,
direction) WHERE application_id IS NULL`. SQL Server needs two because it
treats NULLs as equal inside one unique index; SQLite ignores the filter, so
the guarantee is real only in production. On the cloud database they are
created by `scripts/db.py fresh`; a database that already holds a duplicate
pair from before this rule refuses them until it is reseeded.

> `direction` exists because spec §9 requires two different formulas, and it
> is part of every lookup: a screen must never pair one direction's score
> with the other direction's prose.

**`reasoning_trace`** holds the ordered steps the agent took —
`[{"step": 1, "action": "get_adopter_profile", "detail": "record returned"}, …]`
— including retrievals with their queries, each web-search gate decision with
the rule that made it, tool failures and dropped citations. It is the audit
artefact for blueprint §6.2 and is rendered on `/analyses/<id>` under "How the
agent reasoned". It is **nullable** rather than defaulting to an empty array,
because an analysis written before the agent recorded a trace has none, and an
empty list would claim it reasoned in no steps.

`evidence_sources` entries carry `{"kind": "rag"|"web", "reference": "...",
"cited": true|false}`, so the interface can distinguish a source that was
merely consulted from one the explanation actually relied on (spec §6.4).

**There is no unique constraint on (adopter, animal, direction).** Caching is
enforced by the worker instead: before running an analysis it looks for an
existing row for the same pairing and direction, reuses it when
`generated_at` is later than both `Animal.updated_at` and
`AdopterProfile.updated_at` *and* `model_name` is not
`deterministic-fallback`, and otherwise recomputes and **updates the row in
place**, keeping its `match_analysis_id` so a link to `/analyses/<id>` stays
valid (NFR-3.3).

### 2.8 `notifications`

| Column | Type | Constraints |
|---|---|---|
| `notification_id` | NVARCHAR(36) | PK |
| `user_id` | NVARCHAR(36) | FK → users, NOT NULL |
| `notification_type` | NVARCHAR(50) | NOT NULL, CHECK IN ('INVITATION_RECEIVED','INVITATION_RESPONSE','APPLICATION_STATUS_CHANGED','ANALYSIS_READY') |
| `title` | NVARCHAR(200) | NOT NULL |
| `body` | NVARCHAR(1000) | NOT NULL |
| `link_url` | NVARCHAR(300) | NULL |
| `is_read` | BIT | NOT NULL, Python default 0 |
| `created_at` | DATETIME | NOT NULL |

Index: `ix_notifications_user_read` on `(user_id, is_read)` — the shape the
unread badge queries.

Internal inbox only, no email (spec §23). `link_url` is always a relative path
and is re-checked before it is followed, so a stored value cannot become an
open redirect.

> `ANALYSIS_READY` is declared in the enum and never written. The agent
> completes analyses silently, and the screens that show them poll
> `/api/analysis-status` instead. The value is kept because notifying on a
> completed analysis is a natural extension and removing it would be a schema
> change; it is named here so nobody assumes it fires.

### 2.9 `domain_events` — append-only

| Column | Type | Constraints |
|---|---|---|
| `event_id` | NVARCHAR(36) | PK |
| `event_type` | NVARCHAR(100) | NOT NULL, CHECK `ck_domain_events_event_type` IN the 17 `DomainEventType` values |
| `aggregate_type` | NVARCHAR(50) | NOT NULL, CHECK IN ('Application','Invitation','Animal','AdopterProfile') |
| `aggregate_id` | NVARCHAR(36) | NOT NULL |
| `sequence_number` | INT | NOT NULL |
| `occurred_at` | DATETIME | NOT NULL, naive UTC |
| `actor_user_id` | NVARCHAR(36) | NULL — NULL means the system or the agent |
| `payload` | NVARCHAR(MAX) | NOT NULL, JSON string, default `{}` |

Constraints and indexes:

- `UNIQUE (aggregate_id, sequence_number)` — ordering plus optimistic
  concurrency. `append` derives the next sequence from the aggregate's current
  highest, so two concurrent writers racing on the same aggregate cannot both
  commit.
- `ix_events_aggregate` on `(aggregate_type, aggregate_id, sequence_number)`.
- `ix_events_occurred` on `occurred_at` — powers the dashboard activity feed.

**No UPDATE or DELETE is ever issued against this table.** `EventStore`
exposes `append` plus five read methods and nothing else, so the immutability
is a property of the API rather than a convention.

Payloads are serialized with `json.dumps(default=str)`. That covers the values
that actually appear — datetimes and `Decimal` column values become strings —
and has one sharp edge worth knowing: a `set` becomes its `repr`, not a JSON
array. `tests/integration/test_qa_event_store.py` documents each case.

Satisfies blueprint §10's minimum fields: identifier, type, time,
context/actor, data.

### 2.10 `analysis_jobs` — the agent's work queue

This table is the process boundary between the Flask app and the agent. It has
**no foreign keys**: rows outlive the records they name, and a cascade from a
deleted animal would silently rewrite the queue.

| Column | Type | Constraints |
|---|---|---|
| `analysis_job_id` | NVARCHAR(36) | PK |
| `job_type` | NVARCHAR(40) | NOT NULL, CHECK IN ('RANK_APPLICANT','FIND_MY_PET','FIND_MORE_ADOPTERS','INTERPRET_INTENT') |
| `status` | NVARCHAR(20) | NOT NULL, CHECK IN ('PENDING','IN_PROGRESS','COMPLETED','FAILED') |
| `adopter_profile_id` / `animal_id` / `application_id` | NVARCHAR(36) | NULL, no FK |
| `natural_language_query` | NVARCHAR(1000) | NULL |
| **`result_payload`** | NVARCHAR(MAX) | NULL |
| `attempt_count` | INT | NOT NULL, Python default 0 |
| `error_message` | NVARCHAR(1000) | NULL |
| `created_at` / `started_at` / `completed_at` | DATETIME | NOT NULL / NULL / NULL |

Index: `ix_jobs_status_created` on `(status, created_at)` — **unfiltered**, so
it also serves the stranded-job sweep, which looks for IN_PROGRESS rows older
than 15 minutes, and the status endpoint, which counts COMPLETED and FAILED.

**`natural_language_query` and `result_payload` are the two-way channel for
natural-language search.** The web tier writes the first when it enqueues an
`INTERPRET_INTENT` job; the agent writes the second when it has interpreted it,
as JSON with exactly the keys `understood`, `species`, `size`,
`activity_level`, `temperament`, `good_with_children`,
`good_with_other_animals` and `interpretation`. The web tier reads it back
through `app/domain/search_intent.py`, which validates the stored payload with
the same rules the model's original answer went through — a value that is not
a real `Species` is dropped on the way in *and* on the way out. An analysis job
writes its result to `match_analyses` instead and leaves `result_payload` null.

Of the four job types, **`RANK_APPLICANT` and `INTERPRET_INTENT` are the two
the application enqueues today.** `FIND_MY_PET` and `FIND_MORE_ADOPTERS` are
handled by the worker but nothing enqueues them: both screens rank
deterministically and synchronously, and the explanations they display are the
ones written for applications. The enum values are kept because the worker
maps them and the direction they imply is part of the contract.

---

## 3. State machines

Per spec §25, invitation and application are separate but connected processes.

### Application

```
SUBMITTED ──→ UNDER_REVIEW ──→ APPROVED
    │              │       └──→ REJECTED
    │              │
    ├──────────────┴──→ WITHDRAWN        (adopter)
    └──────────────────→ CLOSED          (another of this adopter's
                                          applications was approved)

APPROVED  ──→ SUBMITTED   (reversal of the approval)
CLOSED    ──→ SUBMITTED   (reopen, only when the approval that caused
                           *this* closure is reversed)
```

Every transition not in the table raises `IllegalTransitionError`.
`tests/unit/test_qa_state_machines.py::test_illegal_move_raises` walks every
ordered pair exhaustively. `CLOSED → SUBMITTED` additionally requires
`may_reopen`: a CLOSED row with no recorded cause, or one closed by a
different approval, is never resurrected by guesswork.

| Transition | Command | Event |
|---|---|---|
| → SUBMITTED | `SubmitApplicationCommand` | `ApplicationSubmitted` |
| → UNDER_REVIEW | `MarkApplicationUnderReviewCommand` | `ApplicationUnderReview` |
| → APPROVED | `ApproveApplicationCommand` | `ApplicationApproved` |
| → REJECTED | `RejectApplicationCommand` | `ApplicationRejected` |
| → WITHDRAWN | `WithdrawApplicationCommand` | `ApplicationWithdrawn` |
| → CLOSED | (cascade inside `ApproveApplicationCommand`) | `ApplicationClosedDueToOtherApproval` |
| → SUBMITTED again | `ReverseApprovalCommand` | `ApplicationReopened` |

### Invitation

```
SENT ──→ VIEWED ──→ ACCEPTED
  │         │   └─→ DECLINED
  └─────────┴─────→ EXPIRED
```

**Accepting an invitation does not approve an adoption** (spec §7.4). It moves
the adopter into the application workflow — an application is created with
`originating_invitation_id` set, and it is refused if the animal is no longer
AVAILABLE, exactly as a direct application would be.

Expiry is **not** a computed property. `ExpireOverdueInvitationsCommand`
writes the status and appends `InvitationExpired`, so a history shows it
happening. Nothing schedules it: it is dispatched from a `before_request` hook
on the Flask app, guarded by a timestamp so it runs at most once every five
minutes however many requests arrive, and skipped for static files. Expiry
therefore happens only while somebody is using the site — acceptable, because
the only thing that observes an expired invitation is a page someone is
looking at. The sweep is idempotent and never touches an answered invitation.

### Animal

```
AVAILABLE ──→ RESERVED ──→ ADOPTION_IN_PROGRESS ──→ ADOPTED
     ↑            │                  │
     └────────────┴──────────────────┘   (reversal on cancellation)
AVAILABLE ←──→ UNAVAILABLE   (medical hold, etc.)
```

Three commands move an animal, and all three are staff-only:

| Command | Route | Event |
|---|---|---|
| `CreateAnimalCommand` | `POST /animals/new` | `AnimalListed` |
| `UpdateAnimalCommand` | `POST /animals/<id>/edit` | `AnimalUpdated` |
| `ChangeAnimalStatusCommand` | `POST /animals/<id>/status` | `AnimalStatusChanged` |

`AnimalStatusChanged` is also appended by the application workflow, with a
null actor when the system moved the animal: approving an application sends it
to ADOPTION_IN_PROGRESS, and reversing that approval returns it to AVAILABLE.

Only `AVAILABLE` is open for applications (`AnimalStatus.is_open_for_applications`),
which is why an adopted animal disappears from the default search while its
details page still loads.

---

## 4. The approval cascade (spec §7.5)

The rule with the most subtle data requirements:

1. Staff approve application *A*, through `POST /applications/<id>/decide`.
2. The handler refuses unless *A*'s current status permits approval and the
   animal is still free to promise — one animal cannot be approved for two
   adopters.
3. `ApplicationApproved(A)` is appended.
4. Every **other active application by that same adopter** is closed, each
   appending `ApplicationClosedDueToOtherApproval` with
   `caused_by_application_id = A`.
5. The projection sets `status='CLOSED'` and
   `closed_because_application_id = A`.
6. The animal on *A* moves to `ADOPTION_IN_PROGRESS`.
7. The adopter is notified.

**Rival applications stay open.** The cascade is scoped by *adopter*, not by
animal: another adopter's application for the same animal is untouched, so a
human can reject it with a reason rather than having the system close it
silently. A second approval for that animal is refused while the first stands.

If *A* is later reversed:

8. The closing events are read back and the latest cause per application is
   taken.
9. Exactly those applications closed *because of A* are reopened, appending
   `ApplicationReopened` and clearing `closed_because_application_id`.
10. Applications withdrawn, rejected, or closed by a different approval are
    untouched.
11. The animal returns to AVAILABLE.

Step 9 is exactly what a current-state-only schema cannot do, and is the
concrete justification for event sourcing recorded in `ARCHITECTURE.md` §4.

---

## 5. Separation from the Vector Database

The Vector DB (ChromaDB, local disk under `data/chroma/`) holds **only** the
curated knowledge base: 17 care guides, species characteristics, compatibility
guidance and organization policy, chunked into 104 passages. It contains **no**
adopter or animal records.

Blueprint §11 requires this separation. Transactional data lives in SQL Server
and reaches the agent through MCP tools, never through vector search — and the
web-search gate refuses a query about our own records for the same reason
(spec §13, rule 3).

---

## 6. The demo data

`scripts/db.py fresh` drops, recreates and seeds. The seed is deliberate
rather than random: it is sized so every dashboard tile is non-zero and every
eligibility rule has something to exclude.

| Table | Rows |
|---|---|
| `animals` | **157**, across eleven kinds |
| `animal_images` | one primary each — 0 animals without an image, 0 with more than one primary |
| `users` | **43** — 40 adopters and 3 staff |
| `adopter_profiles` | **40**, of which **3 are deliberately incomplete** |
| `adoption_applications` | **81**, including 3 closed by the §7.5 cascade |
| `adoption_invitations` | **32**, covering every status including EXPIRED |
| `notifications` | 48 |
| `match_analyses` | 40, all honest `deterministic-fallback` rows — no model is claimed for text no model wrote |
| `domain_events` | **263** |

Species: DOG 48, CAT 35, BIRD 19, RABBIT 11, GUINEA_PIG 8, HAMSTER 7, OTHER 29
(exotic mammals 11, reptiles 8, farm 6, poultry 3, amphibian 1). Statuses:
AVAILABLE 116, RESERVED 13, ADOPTION_IN_PROGRESS 11, ADOPTED 11, UNAVAILABLE 6.
33 animals (21%) have special needs; 8 are bonded pairs, pinned AVAILABLE so
they are always demonstrable.

### Demo accounts

Password `Password123!` for both.

| Role | Email |
|---|---|
| Staff | `dana@petmatch.org` |
| Adopter | `maya@example.com` |

**The demo story is seeded, not improvised.** Maya has an **APPROVED**
application for **Smaug**, a bearded dragon, with **three other applications
CLOSED by the §7.5 cascade** — so the cascade, the causation column, the
reopen rule and the adopter-visible history all have real data behind them
before anybody touches a button.

**Every animal gets a photograph, even with no network.** The seed fetches a
breed-accurate image where it can, and falls back to a committed placeholder
under `app/static/uploads/` (sample.jpg) where it cannot — so a fresh clone
offline still satisfies spec §24. The seed **fails loudly** if any animal
would be written without an image rather than quietly producing a roster that
breaks the rule the application enforces on every other write path.

> Reseed with `scripts/db.py fresh`, never `seed` over existing data:
> `users.email` is unique and a second seed collides. A full seed takes
> **9–12 minutes**, because it fetches a breed-accurate photograph for every
> animal from free public APIs and paces itself to stay inside their rate
> limits. Do not start one during a presentation.

`tests/unit/test_seed_data.py` tests the seed definitions as pure data — 35
tests covering uniqueness, completeness, the validators, and whether every
string fits the column width it will be written into.
