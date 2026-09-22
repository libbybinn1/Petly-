# MODEL_DATA — PetMatch

**Entities, relationships, keys and constraints.**
Target engine: **Microsoft SQL Server 2014 Express (SP3)** on Somee.com.
Sources: blueprint §11; PetMatch spec §5, §18, §25.

---

## 0. Engine constraints that shaped this schema

SQL Server 2014 is an old engine. These are not preferences — they are hard
limits that dictated concrete choices below.

| Limitation | Available from | Consequence here |
|---|---|---|
| No native `JSON` type or functions | 2016 | `payload`, `reasons`, `concerns`, `evidence` are `NVARCHAR(MAX)`, serialized in Python. No `JSON_VALUE`, `OPENJSON`, `ISJSON`. |
| No `STRING_AGG` | 2017 | String aggregation happens in Python. |
| No `SEQUENCE` on some Somee plans | — | Aggregate identifiers are application-generated `UNIQUEIDENTIFIER` (UUID4). |
| Free-tier size cap | — | Images are files on disk; the DB stores only a URL. No BLOBs. |
| `OFFSET ... FETCH NEXT` **is** supported | 2012 | Keyset pagination is fine. |
| `DATETIME2` **is** supported | 2008 | All timestamps are `DATETIME2`, stored UTC. |

**UUID keys also suit event sourcing:** an aggregate's identifier must exist
*before* the first event is appended, so the application cannot wait for a
database-generated identity value.

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
        └──< DomainEvent (actor)

      Animal (1) ──< AnimalImage
      AdoptionApplication (1) ──< AnalysisJob
```

---

## 2. Tables

### 2.1 `users`

| Column | Type | Constraints |
|---|---|---|
| `user_id` | UNIQUEIDENTIFIER | PK |
| `email` | NVARCHAR(254) | NOT NULL, UNIQUE |
| `password_hash` | NVARCHAR(255) | NOT NULL |
| `full_name` | NVARCHAR(150) | NOT NULL |
| `role` | NVARCHAR(20) | NOT NULL, CHECK IN ('ADOPTER','STAFF') |
| `is_active` | BIT | NOT NULL, DEFAULT 1 |
| `created_at` | DATETIME2 | NOT NULL |

Index: `UQ_users_email` on `email`.

> Two roles satisfy blueprint §3. `is_active` participates in the eligibility
> rules of spec §10.

### 2.2 `adopter_profiles`

One-to-one with a user of role `ADOPTER`.

| Column | Type | Constraints |
|---|---|---|
| `adopter_profile_id` | UNIQUEIDENTIFIER | PK |
| `user_id` | UNIQUEIDENTIFIER | FK → users, UNIQUE, NOT NULL |
| `home_type` | NVARCHAR(20) | CHECK IN ('APARTMENT','HOUSE','FARM') |
| `has_yard` | BIT | NOT NULL |
| `yard_size_sqm` | INT | NULL, CHECK >= 0 |
| `household_has_children` | BIT | NOT NULL |
| `youngest_child_age` | INT | NULL, CHECK BETWEEN 0 AND 18 |
| `has_other_animals` | BIT | NOT NULL |
| `other_animals_description` | NVARCHAR(500) | NULL |
| `experience_level` | NVARCHAR(20) | CHECK IN ('NONE','SOME','EXPERIENCED') |
| `activity_level` | NVARCHAR(20) | CHECK IN ('LOW','MODERATE','HIGH') |
| `daily_hours_available` | DECIMAL(4,1) | CHECK BETWEEN 0 AND 24 |
| `city` | NVARCHAR(100) | NOT NULL |
| `preferred_species` | NVARCHAR(200) | comma-separated enum values |
| `preferred_size` | NVARCHAR(20) | NULL |
| `preferred_age_range` | NVARCHAR(20) | NULL |
| **`open_to_proactive_suggestions`** | BIT | **NOT NULL, DEFAULT 0** |
| `is_complete` | BIT | NOT NULL, DEFAULT 0 |
| `created_at` / `updated_at` | DATETIME2 | NOT NULL |

> `open_to_proactive_suggestions` is required by spec §5.1 and is a hard
> eligibility gate for Find More Adopters (spec §10). `is_complete` is another.

### 2.3 `animals`

| Column | Type | Constraints |
|---|---|---|
| `animal_id` | UNIQUEIDENTIFIER | PK |
| `name` | NVARCHAR(100) | NOT NULL |
| `species` | NVARCHAR(30) | NOT NULL, CHECK IN ('DOG','CAT','RABBIT','HAMSTER','GUINEA_PIG','BIRD','OTHER') |
| `breed` | NVARCHAR(100) | NULL |
| `age_years` | DECIMAL(4,1) | NOT NULL, CHECK >= 0 |
| `size` | NVARCHAR(20) | NOT NULL, CHECK IN ('SMALL','MEDIUM','LARGE') |
| `temperament` | NVARCHAR(30) | CHECK IN ('CALM','BALANCED','ENERGETIC','ANXIOUS') |
| `activity_level` | NVARCHAR(20) | CHECK IN ('LOW','MODERATE','HIGH') |
| `good_with_children` | BIT | NOT NULL |
| `good_with_other_animals` | BIT | NOT NULL |
| `has_special_needs` | BIT | NOT NULL, DEFAULT 0 |
| `special_needs_description` | NVARCHAR(1000) | NULL |
| `required_space` | NVARCHAR(20) | CHECK IN ('SMALL','MEDIUM','LARGE') |
| `city` | NVARCHAR(100) | NOT NULL |
| `status` | NVARCHAR(30) | NOT NULL, CHECK IN ('AVAILABLE','RESERVED','ADOPTION_IN_PROGRESS','ADOPTED','UNAVAILABLE') |
| `description` | NVARCHAR(MAX) | NULL |
| `created_at` / `updated_at` | DATETIME2 | NOT NULL |

Indexes: `IX_animals_status_species`, `IX_animals_city`.

### 2.4 `animal_images`

Spec §24 makes images mandatory — every animal must have at least one.

| Column | Type | Constraints |
|---|---|---|
| `animal_image_id` | UNIQUEIDENTIFIER | PK |
| `animal_id` | UNIQUEIDENTIFIER | FK → animals, ON DELETE CASCADE |
| `image_url` | NVARCHAR(500) | NOT NULL |
| `is_primary` | BIT | NOT NULL, DEFAULT 0 |
| `display_order` | INT | NOT NULL, DEFAULT 0 |
| `uploaded_at` | DATETIME2 | NOT NULL |

Filtered unique index enforces one primary image per animal:
`CREATE UNIQUE INDEX UQ_animal_primary_image ON animal_images(animal_id) WHERE is_primary = 1`

> The "at least one image" rule cannot be a table constraint (it is a
> cross-table cardinality rule). It is enforced in the domain layer and by an
> integration test.

### 2.5 `adoption_applications`

| Column | Type | Constraints |
|---|---|---|
| `application_id` | UNIQUEIDENTIFIER | PK |
| `adopter_profile_id` | UNIQUEIDENTIFIER | FK, NOT NULL |
| `animal_id` | UNIQUEIDENTIFIER | FK, NOT NULL |
| `status` | NVARCHAR(30) | NOT NULL, CHECK IN ('SUBMITTED','UNDER_REVIEW','APPROVED','REJECTED','WITHDRAWN','CLOSED') |
| `applicant_message` | NVARCHAR(2000) | NULL |
| `originating_invitation_id` | UNIQUEIDENTIFIER | FK → adoption_invitations, NULL |
| `closed_because_application_id` | UNIQUEIDENTIFIER | FK → self, NULL |
| `submitted_at` | DATETIME2 | NOT NULL |
| `decided_at` | DATETIME2 | NULL |
| `decided_by_user_id` | UNIQUEIDENTIFIER | FK → users, NULL |

Filtered unique index prevents duplicate *active* applications while still
allowing a re-application after a rejection:

```sql
CREATE UNIQUE INDEX UQ_active_application
  ON adoption_applications(adopter_profile_id, animal_id)
  WHERE status IN ('SUBMITTED','UNDER_REVIEW');
```

> `closed_because_application_id` is the **projection** of the
> `ApplicationClosedDueToOtherApproval` event. It exists for query speed; the
> event log remains the source of truth. This is the column that makes spec
> §7.5's reopen rule mechanical rather than guesswork.

### 2.6 `adoption_invitations`

| Column | Type | Constraints |
|---|---|---|
| `invitation_id` | UNIQUEIDENTIFIER | PK |
| `animal_id` | UNIQUEIDENTIFIER | FK, NOT NULL |
| `adopter_profile_id` | UNIQUEIDENTIFIER | FK, NOT NULL |
| `sent_by_user_id` | UNIQUEIDENTIFIER | FK → users (STAFF), NOT NULL |
| `status` | NVARCHAR(20) | NOT NULL, CHECK IN ('SENT','VIEWED','ACCEPTED','DECLINED','EXPIRED') |
| `staff_message` | NVARCHAR(1000) | NULL |
| `sent_at` | DATETIME2 | NOT NULL |
| `expires_at` | DATETIME2 | NOT NULL |
| `viewed_at` / `responded_at` | DATETIME2 | NULL |

`expires_at = sent_at + 72 hours` (spec §7.4). All timestamps are timezone-aware
UTC; ruff's `DTZ` rules prevent naive datetimes because this expiry depends on it.

### 2.7 `match_analyses`

| Column | Type | Constraints |
|---|---|---|
| `match_analysis_id` | UNIQUEIDENTIFIER | PK |
| `direction` | NVARCHAR(20) | NOT NULL, CHECK IN ('ADOPTER_TO_ANIMAL','ANIMAL_TO_ADOPTER') |
| `adopter_profile_id` | UNIQUEIDENTIFIER | FK, NOT NULL |
| `animal_id` | UNIQUEIDENTIFIER | FK, NOT NULL |
| `application_id` | UNIQUEIDENTIFIER | FK, NULL |
| `score` | INT | NOT NULL, CHECK BETWEEN 0 AND 100 |
| `is_disqualified` | BIT | NOT NULL, DEFAULT 0 |
| `criterion_scores` | NVARCHAR(MAX) | JSON string — per-criterion breakdown |
| `reasons` | NVARCHAR(MAX) | JSON string array |
| `concerns` | NVARCHAR(MAX) | JSON string array |
| `missing_information` | NVARCHAR(MAX) | JSON string array |
| `evidence_sources` | NVARCHAR(MAX) | JSON string array — RAG chunk ids and web URLs |
| `used_web_search` | BIT | NOT NULL, DEFAULT 0 |
| `model_name` | NVARCHAR(100) | NOT NULL |
| `generated_at` | DATETIME2 | NOT NULL |

> `direction` exists because spec §9 requires two different formulas.
> `evidence_sources` and `used_web_search` implement the source-documentation
> requirement of spec §6.4 and §13.

### 2.8 `notifications`

| Column | Type | Constraints |
|---|---|---|
| `notification_id` | UNIQUEIDENTIFIER | PK |
| `user_id` | UNIQUEIDENTIFIER | FK, NOT NULL |
| `notification_type` | NVARCHAR(50) | NOT NULL |
| `title` | NVARCHAR(200) | NOT NULL |
| `body` | NVARCHAR(1000) | NOT NULL |
| `link_url` | NVARCHAR(300) | NULL |
| `is_read` | BIT | NOT NULL, DEFAULT 0 |
| `created_at` | DATETIME2 | NOT NULL |

Internal inbox only — no email (spec §23).

### 2.9 `domain_events` — append-only

| Column | Type | Constraints |
|---|---|---|
| `event_id` | UNIQUEIDENTIFIER | PK |
| `event_type` | NVARCHAR(100) | NOT NULL |
| `aggregate_type` | NVARCHAR(50) | NOT NULL |
| `aggregate_id` | UNIQUEIDENTIFIER | NOT NULL |
| `sequence_number` | INT | NOT NULL |
| `occurred_at` | DATETIME2 | NOT NULL |
| `actor_user_id` | UNIQUEIDENTIFIER | FK → users, NULL (NULL = system/agent) |
| `payload` | NVARCHAR(MAX) | NOT NULL, JSON string |

Constraints and indexes:
- `UNIQUE (aggregate_id, sequence_number)` — ordering plus optimistic concurrency.
- `IX_events_aggregate` on `(aggregate_type, aggregate_id, sequence_number)`.
- `IX_events_occurred` on `occurred_at` — powers the dashboard activity feed.

**No UPDATE or DELETE is ever issued against this table.** The repository
exposes only `append()` and read methods; there is no update path to misuse.

Satisfies blueprint §10's minimum fields: identifier, type, time, context/actor,
data.

### 2.10 `analysis_jobs` — the agent's work queue

This table is the process boundary between the Flask app and the agent.

| Column | Type | Constraints |
|---|---|---|
| `analysis_job_id` | UNIQUEIDENTIFIER | PK |
| `job_type` | NVARCHAR(40) | CHECK IN ('RANK_APPLICANT','FIND_MY_PET','FIND_MORE_ADOPTERS','INTERPRET_INTENT') |
| `status` | NVARCHAR(20) | CHECK IN ('PENDING','IN_PROGRESS','COMPLETED','FAILED') |
| `adopter_profile_id` / `animal_id` / `application_id` | UNIQUEIDENTIFIER | FK, NULL |
| `natural_language_query` | NVARCHAR(1000) | NULL |
| `attempt_count` | INT | NOT NULL, DEFAULT 0 |
| `error_message` | NVARCHAR(1000) | NULL |
| `created_at` / `started_at` / `completed_at` | DATETIME2 | |

Index `IX_jobs_pending ON analysis_jobs(status, created_at) WHERE status = 'PENDING'`
so the agent's poll is cheap.

---

## 3. State machines

Per spec §25, invitation and application are separate but connected processes.

### Application

```
SUBMITTED ──→ UNDER_REVIEW ──→ APPROVED
    │              │       └──→ REJECTED
    │              │
    ├──────────────┴──→ WITHDRAWN        (adopter)
    └──────────────────→ CLOSED          (another application approved)

CLOSED ──→ SUBMITTED    (reopen, only when the causing approval is reversed)
```

### Invitation

```
SENT ──→ VIEWED ──→ ACCEPTED
  │         │   └─→ DECLINED
  └─────────┴─────→ EXPIRED   (automatic at sent_at + 72h)
```

**Accepting an invitation does not approve an adoption** (spec §7.4). It moves
the adopter into the application workflow — an application is created with
`originating_invitation_id` set.

### Animal

```
AVAILABLE ──→ RESERVED ──→ ADOPTION_IN_PROGRESS ──→ ADOPTED
     ↑            │                  │
     └────────────┴──────────────────┘   (reversal on cancellation)
AVAILABLE ←──→ UNAVAILABLE   (medical hold, etc.)
```

---

## 4. The approval cascade (spec §7.5)

The rule with the most subtle data requirements:

1. Staff approve application *A*.
2. `ApplicationApproved(A)` is appended.
3. Every **other active** application by that adopter is closed — each appending
   `ApplicationClosedDueToOtherApproval(caused_by=A)`.
4. The projection sets `status='CLOSED'` and `closed_because_application_id=A`.
5. The animal on *A* moves to `ADOPTION_IN_PROGRESS`.

If *A* is later cancelled or withdrawn:

6. Query for applications where `closed_because_application_id = A`.
7. Those — **and only those** — are eligible to reopen, appending
   `ApplicationReopened`.
8. Applications closed for any other reason are untouched.

Step 7 is exactly what a current-state-only schema cannot do, and is the
concrete justification for event sourcing recorded in `ARCHITECTURE.md` §4.

---

## 5. Separation from the Vector Database

The Vector DB (ChromaDB, local disk) holds **only** the curated knowledge base:
care guides, species characteristics, compatibility guidance, organization
policy. It contains **no** adopter or animal records.

Blueprint §11 requires this separation. Transactional data lives in SQL Server
and reaches the agent through MCP tools, never through vector search.
