# ARCHITECTURE — PetMatch

**MVC, CQRS, Event Sourcing, the Agent, MCP, and system boundaries.**
Sources: course blueprint §9, §10, §11; PetMatch spec §11, §18, §19.

This document describes the code as it is. Where a layer is a stub, or a rule
is enforced by review rather than by a test, it says so.

---

## 1. System overview

```
                         ┌─────────────────────────┐
                         │        Browser          │
                         │   (Jinja2-rendered)     │
                         └────────────┬────────────┘
                                      │ HTTP
                         ┌────────────▼────────────┐
                         │      FLASK APP          │   process 1
                         │                         │
                         │  controllers/   (C)     │
                         │  templates/     (V)     │
                         │  domain/        (M)     │
                         │  cqrs/  commands|queries│
                         │  eventstore/            │
                         │  infrastructure/        │
                         │  security/              │
                         └────────────┬────────────┘
                                      │ SQLAlchemy / pymssql
                         ┌────────────▼────────────┐
                         │  Somee.com SQL Server   │
                         │  2014 (cloud, source of │
                         │   truth for business    │
                         │   data + event log)     │
                         └────────────┬────────────┘
                                      │ analysis_jobs table (queue)
                                      │ polled, not called
                         ┌────────────▼────────────┐
                         │     AGENT SERVICE       │   process 2
                         │                         │
                         │  worker.py  the queue   │
                         │  loop.py    reason/act  │
                         │  rag/       ChromaDB    │
                         │  tools/     web + stdio │
                         └──────┬───────────┬──────┘
                                │ stdio     │ HTTPS
                     ┌──────────▼──────┐  ┌─▼──────────┐
                     │  MCP SERVER     │  │  Tavily    │
                     │  (process 3)    │  │  Web Search│
                     │ get_adopter_... │  └────────────┘
                     │ get_animal_...  │
                     └─────────┬───────┘
                               │ read-only
                     ┌─────────▼───────┐   ┌──────────────┐
                     │  SQL Server     │   │  ChromaDB    │
                     │  (same cloud DB)│   │  (local disk)│
                     └─────────────────┘   └──────────────┘
```

**Three OS processes.** The agent is not an import; it is a separate program
with its own lifecycle, started by `python -m agent_service`. The MCP server
is a third process that the agent spawns and talks to over stdin/stdout.

Note that the deterministic scorer, `app/domain/matching.py`, is **shared**
between processes 1 and 2 rather than duplicated. It imports nothing but the
standard library and its own package, so both tiers can compute the same
number from the same code.

---

## 2. MVC

Blueprint §9.1 requires clearly defined Controller / View / Model
responsibilities with no mixing.

### Model — `app/domain/` and `app/infrastructure/`

`app/domain/` holds value objects, enums and business rules as plain Python:
`matching.py`, `application_rules.py`, `invitation_rules.py`,
`profile_rules.py`, `animal_rules.py`, `search_intent.py`, `facts.py`,
`enums.py`. **It
imports no framework** — no Flask, no SQLAlchemy, nothing from a layer above
it. This is deliberate and mechanically enforced by
`tests/unit/test_architecture_guard.py::test_domain_layer_imports_no_framework_or_upper_layer`:
a domain unit test runs with no application context and no database.

`app/domain/facts.py` owns **fact mapping**: the one converter pair that turns
a stored adopter row and a stored animal row into the `AdopterFacts` and
`AnimalFacts` the scorer consumes, from an ORM row or from an MCP tool's JSON
payload. It lives in the domain, and not in a query module, because three
callers need it — the web tier when it ranks, the agent when it
scores an MCP payload, and the seed script when it writes demonstration
analyses. Three private copies is how the same corrupt species value comes out
as `OTHER` in one place and as nothing in another.

`app/infrastructure/` holds the SQLAlchemy models, the engine and the session
factory. It is persistence, not business logic. It also owns `utc_now()`: the
single place where an aware datetime becomes the naive UTC value a
SQL Server 2014 `DATETIME` column stores. Having one such place is what makes
"every timestamp in the database is UTC" a fact rather than a habit.

> **`app/repositories/`, `app/services/` and `app/views/` are empty stubs.**
> Each contains only a package docstring. They were created when the layout
> was first sketched and never filled, because the CQRS handlers turned out
> to be the right place for data access and the query modules the right place
> for view models. They are named here rather than quietly left in the tree,
> because "so where is your repository layer?" deserves a straight answer:
> there is not one, and the work it would have done is described below.

### View — `app/templates/` plus the view models in `app/cqrs/queries/`

Jinja2 templates live in `app/templates/`. The dataclasses passed into them —
`AnimalCard`, `DashboardSummary`, `StatTile`, `RankedCandidate`,
`MatchAnalysisDetail`, `InterpretedIntentView`, `DisplayDate` — live beside
the query that builds them, in `app/cqrs/queries/*.py`.

Templates **display**; they do not decide. Anything conditional that expresses
a business rule is computed in the domain or query layer and arrives on the
view model as a boolean or a label: `AnimalCard.has_match_score`,
`RankedCandidate.can_approve`, `StatTile.action_url`,
`DisplayDate.relative_label`. Templates still branch on presence — "is there a
score to show?" — which is display logic, not a rule.

### Controller — `app/controllers/`

Six Flask blueprints: `home`, `auth`, `animals`, `matches`, `personal`,
`dashboard`. A controller does exactly four things, in order:

1. Parse the HTTP request into a typed command or query.
2. Check authorization, through `@require_sign_in`, `@require_staff()` and
   `@require_adopter()`.
3. Dispatch it on the bus.
4. Render a template or return a response.

**No controller holds a database session.** Only
`app/controllers/helpers.py` may name SQLAlchemy at all — it hands out the
session factory for the one caller that needs it — and
`tests/unit/test_architecture_guard.py::test_controllers_never_hold_a_database_session`
fails the build otherwise. The authentication controller was the last
exception and lost it: verifying a password is a query with an unusual result,
not a different kind of thing, and it now goes through
`GetAccountForSignInQuery` like everything else
(`tests/api/test_feature_auth_boundary.py` checks that structurally).

Any `if` expressing a *business* rule in a controller is a bug.

---

## 3. CQRS

Blueprint §9.2 requires conceptual **and implementation** separation between
operations that modify information and operations that read it.

### The real tree

```
app/cqrs/
├── base.py                          Command, Query, CommandHandler[T],
│                                    QueryHandler[T], MessageBus
├── commands/
│   ├── account_commands.py          RegisterAdopterCommand
│   ├── analysis_commands.py         EnqueueIntentInterpretationCommand
│   ├── animal_commands.py           CreateAnimalCommand, UpdateAnimalCommand,
│   │                                ChangeAnimalStatusCommand
│   ├── application_commands.py      SubmitApplicationCommand, WithdrawApplicationCommand,
│   │                                MarkApplicationUnderReviewCommand,
│   │                                ApproveApplicationCommand, RejectApplicationCommand,
│   │                                ReverseApprovalCommand
│   ├── invitation_commands.py       SendInvitationCommand, MarkInvitationViewedCommand,
│   │                                RespondToInvitationCommand,
│   │                                ExpireOverdueInvitationsCommand
│   ├── notification_commands.py     MarkNotificationReadCommand,
│   │                                MarkAllNotificationsReadCommand
│   └── profile_commands.py          SaveAdopterProfileCommand
└── queries/
    ├── analysis_status_queries.py   GetAnalysisStatusQuery
    ├── animal_queries.py            SearchAnimalsQuery, GetAnimalDetailsQuery,
    │                                ListAllAnimalsQuery, AnimalCard
    ├── auth_queries.py              GetAccountForSignInQuery, EmailIsRegisteredQuery
    ├── dashboard_queries.py         GetDashboardSummaryQuery, DashboardSummary, StatTile
    ├── formatting.py                DisplayDate, to_display_date, to_display_moment
    ├── history_queries.py           GetAggregateHistoryQuery
    ├── intent_queries.py            GetIntentJobQuery, InterpretedIntentView
    ├── match_queries.py             FindMyPetQuery, FindMyPetWithIntentQuery,
    │                                RankApplicantsQuery, FindMoreAdoptersQuery,
    │                                GetMatchAnalysisQuery
    ├── notification_queries.py      ListMyNotificationsQuery
    ├── personal_queries.py          ListMyApplicationsQuery, ListMyInvitationsQuery,
    │                                CountUnreadNotificationsQuery
    └── profile_queries.py           GetMyProfileQuery
```

18 command handlers and 19 query handlers are registered on the bus in
`app/__init__.py`.

### The contract

| | Command | Query |
|---|---|---|
| Purpose | change state | read state |
| Returns | an identifier, a count of what it affected, or nothing | a read DTO |
| Side effects | appends domain events | none |
| Transaction | committed | rolled back |
| Idempotent | no | yes |

**A command returns an identifier, a count of what it affected, or nothing —
never read data.** When a screen needs data after a write, the controller
dispatches a query afterwards — `personal.my_invitations` marks
invitations viewed and then re-queries, which is the CQRS-correct way to get
the statuses it just wrote.

Four handlers return an `int`, and the rule permits it because a count is not
read data — it is how many rows the write touched:
`ApproveApplicationHandler` (applications closed by the cascade),
`ReverseApprovalHandler` (applications reopened),
`ExpireOverdueInvitationsHandler` (invitations expired) and
`MarkAllNotificationsReadHandler` (messages marked). Each drives a flash
message, never a screen. `tests/unit/test_architecture_guard.py::test_command_handlers_never_return_read_data`
allows exactly `str`, `int`, `None` and `str | None`, and flags a dataclass or
a list.

### Where the session lives

`MessageBus` opens a session per dispatch and owns the transaction. A command
dispatch commits; a **query dispatch rolls back** (`app/cqrs/base.py`), which
makes an accidental write in a read path impossible to persist rather than
merely discouraged. The handler receives the session as an argument, so the
handler is the layer that holds it — not the controller, and not a repository,
because there is not one.

Two tests keep this honest:
`test_query_modules_never_write` flags a `session.commit()`, a `session.add()`,
a `session.execute(update(...))`, a bare DML import or a `.delete()` chained
onto a select, anywhere under `app/cqrs/queries/`. `test_controllers_never_hold_a_database_session`
does the same for controllers. `scripts/verify_requirements.py` repeats both
from the command line, importing the same helpers so the two copies cannot
drift.

### Why CQRS suits this system

PetMatch is heavily read-skewed. Search, the dashboard, applicant rankings,
the staff table and the personal area all read, and each wants
differently-shaped data: a search result is a card, a dashboard figure is a
tile with a link, a ranking row carries a score, four permission booleans and
a queue timestamp. Writes are few and sharply defined — submit, withdraw,
approve, reject, review, reverse, invite, respond, list an animal, change its
status — and each carries real business rules that must run in one
transaction with its event. Forcing both through one model would compromise
both: the write model would grow display concerns, and the read model would
carry rules it never applies.

---

## 4. Event Sourcing

Blueprint §10 requires event sourcing with a documented justification.

### Why event sourcing is right for *this* process

Spec §7.5 states the rule that makes it necessary:

> If one application is approved, the other applications should be closed as
> no longer active... However, the system should preserve history. If the
> approved adoption is later cancelled or the adopter withdraws before the
> process is finalized, the previous applications should not be silently
> deleted. Their historical state remains available, and business rules can
> allow appropriate applications to be reopened.

Consider an adopter with three active applications. One is approved; the other
two close. Two weeks later the adoption falls through.

**With current-state storage only**, those two applications are rows reading
`status = CLOSED`. Nothing records *why* they closed. Were they closed because
another application was approved — in which case reopening is correct — or
because the adopter withdrew, or staff rejected them? The information needed
to decide has been overwritten. Reopening becomes a guess.

**With an event log**, each closure carries
`ApplicationClosedDueToOtherApproval` with `caused_by_application_id` in its
payload. When that approval is reversed, the system knows exactly which
applications closed because of it and reopens precisely those.

That is not a hypothetical. `ReverseApprovalHandler` builds its reopen set by
calling `_closure_causes_from_events`, which reads every
`ApplicationClosedDueToOtherApproval` event out of the store and keeps the
**latest** cause per application. The latest matters: an application closed by
approval A, reopened when A was reversed, then closed again by approval B is
currently closed *by B*, and reversing A a second time must not resurrect it.
Events arrive oldest first, so later entries overwrite earlier ones. The
`closed_because_application_id` column still exists, but it is now a
cross-check rather than the authority.

`tests/integration/test_event_sourcing.py::TestReopeningFromTheEventLog`
proves all three cases, including
`test_the_most_recent_closure_decides_what_a_reversal_reopens`.

### Implementation

Append-only table, never updated or deleted. `EventStore` exposes `append`
and four read methods — `read_aggregate_stream`, `read_all`, `read_recent`,
`read_events_of_type` — and `count`. There is deliberately no update or
delete method, so the immutability of the log is a property of the API rather
than a convention people must remember.

| Column | Type | Purpose |
|---|---|---|
| `event_id` | NVARCHAR(36) | Primary key, a UUID4 string |
| `event_type` | NVARCHAR(100) | e.g. `ApplicationApproved` |
| `aggregate_type` | NVARCHAR(50) | `Application`, `Invitation`, `Animal`, `AdopterProfile` |
| `aggregate_id` | NVARCHAR(36) | Which instance |
| `sequence_number` | INT | Order within the aggregate; optimistic concurrency |
| `occurred_at` | DATETIME | **Naive UTC** — see below |
| `actor_user_id` | NVARCHAR(36) | Who caused it (context/actor); NULL means the system or the agent |
| `payload` | NVARCHAR(MAX) | JSON-serialized event data |

This satisfies blueprint §10's minimum of identifier, type, time,
context/actor and data.

**Timestamps are naive UTC, not timezone-aware.** SQL Server 2014's
`DATETIME` carries no offset, so `EventStore.append` takes an aware value,
strips the tzinfo with `.replace(tzinfo=None)` and stores it; every read
attaches UTC again on the way out. Ruff's `DTZ` rules keep the inbound value
aware so the conversion is the only place naivety can enter.
`tests/integration/test_qa_event_store.py::test_an_aware_timestamp_is_stored_naive_and_read_back_aware`
pins the round trip.

> **SQL Server 2014 constraint:** no native JSON type — that arrived in 2016.
> `payload` is `NVARCHAR(MAX)` serialized in Python with `json.dumps(default=str)`.
> No `JSON_VALUE` or `OPENJSON` may be used anywhere.

**There is no Alembic.** The schema is created from the SQLAlchemy metadata
by `scripts/db.py create` / `reset` / `fresh`. One developer, one database and
a rebuild that takes a minute make a migration history a cost with no payer.
`requirements.txt` still pins Alembic; that pin is vestigial.

### Event catalogue

Every member of `DomainEventType` (`app/domain/enums.py`), which is the
authoritative catalogue —
`tests/unit/test_architecture_guard.py::test_every_domain_event_type_is_referenced_somewhere`
proves none of them is declared and then forgotten, and
`tests/integration/test_qa_event_store.py::test_every_event_type_the_code_appends_is_in_the_enum`
proves the code appends nothing outside it.

| Event | Aggregate |
|---|---|
| `ApplicationSubmitted` | Application |
| `ApplicationUnderReview` | Application |
| `ApplicationApproved` | Application |
| `ApplicationRejected` | Application |
| `ApplicationWithdrawn` | Application |
| `ApplicationClosedDueToOtherApproval` | Application |
| `ApplicationReopened` | Application |
| `InvitationSent` | Invitation |
| `InvitationViewed` | Invitation |
| `InvitationAccepted` | Invitation |
| `InvitationDeclined` | Invitation |
| `InvitationExpired` | Invitation |
| `AIAnalysisCompleted` | Application |
| `AnimalListed` | Animal |
| `AnimalUpdated` | Animal |
| `AnimalStatusChanged` | Animal |
| `AdopterProfileUpdated` | AdopterProfile |

Seventeen event types across four aggregate types.

### Projections and replay

Current-state tables are maintained alongside the log as events are appended —
spec §18 explicitly permits this, and a read model that has to replay a log is
not a read model. `app/eventstore/projections.py` is the other direction:
given the events, what *should* those rows say (FR-13.3)?

| Function | What it does |
|---|---|
| `replay_application(events)` | Rebuilds one application's status, animal, closure cause, decider and timestamps from its own stream. Raises `EmptyStreamError` for an aggregate with no events — a never-created aggregate has nothing to rebuild, and a default-valued projection would be a fabrication. |
| `replay_invitation(events)` | The same for one invitation. |
| `rebuild_projections(session, apply=False)` | Replays **every** stream and compares it with the stored row, returning a `RebuildReport` of `ProjectionMismatch` entries, one per disagreeing field. |

Two deliberate limits, stated because they are limits:

- **Applications and Invitations only.** `REBUILDABLE_AGGREGATES` names those
  two, and `EventStore.read_all` takes an `aggregate_types` filter so a
  rebuild reads only those streams rather than the whole log. Animals and adopter profiles are recorded in the log but are not
  event-sourced projections: an animal's current record is edited directly,
  and `AnimalUpdated` carries the change rather than the whole state.
- **Read-only by default.** `rebuild_projections` writes nothing unless
  called with `apply=True`, and even then it commits nothing — the caller owns
  the transaction, so a repair either lands completely or not at all.

This matters for three reasons. It **proves the log is complete**: if replaying
every stream reproduces every row, nothing has been written to a row that was
not also recorded as an event. It **makes the §7.5 rule inspectable**, by
letting a drift between the closing events and the projection column be found
rather than silently reopening the wrong applications. And it is a **repair
tool**.

`tests/integration/test_event_sourcing.py` proves each of those, including
`test_a_consistent_world_reports_no_mismatches`,
`test_a_corrupted_row_is_detected` and `test_applying_a_rebuild_repairs_the_row`.

There is no CLI entry point for a rebuild yet; it is called from tests and
from a Python shell. See `docs/DEMO.md` for the five-line snippet.

---

## 5. The Agent

Spec §11 and blueprint §6 require a real agent — planning, tool selection,
observation and re-planning — not a single LLM call. `docs/AGENT.md` is the
full account; this section is the architectural boundary.

### Independence

The agent is `process 2`, started with `python -m agent_service`. It receives
work by polling the `analysis_jobs` table and obtains domain records through
MCP tools over stdio.

What it may import is stated precisely by CLAUDE.md R2 and checked by
`tests/unit/test_architecture_guard.py::test_agent_and_mcp_modules_never_import_the_web_tier`:

| Allowed from `app` | Why |
|---|---|
| `app.domain` | The scorer and the enums are the shared vocabulary; duplicating them would let the two tiers disagree about a number. |
| `app.config` | One place loads the environment. |
| `app.infrastructure.models` / `.database` | The queue *is* a database table, so it has to be read. |
| `app.eventstore` | The agent appends `AIAnalysisCompleted`. |

| Forbidden | Why |
|---|---|
| The Flask factory (`app/__init__.py`) | It would make the agent a library of the web tier. |
| `app.controllers` | HTTP is not the agent's concern. |
| `app.cqrs` | **This is the one that matters.** With no access to a command, there is no code path by which the agent could approve, reject or alter an application (rule R4, spec §6.4). |
| `app.security` | Authorization is a web concern. |

The boundary holds in the other direction too: **nothing under `app/` imports
`agent_service`.** The one contract the two tiers share — the parsed search
intent and its stored JSON payload — lives in `app/domain/search_intent.py`,
so the web tier can read an interpretation back without reaching into the
agent's package. `tests/api/test_feature_natural_language_search.py::test_no_module_under_app_imports_the_language_model_client`
adds the sharper claim: no module under `app/` can even reach the model
client, so an inference call in a request path is not merely discouraged, it
is unavailable.

Two consequences of the process split: the web application stays responsive
while analysis runs, and the agent can be stopped, restarted or run on
another machine without touching the web tier.

### Division of labour: math vs language

This is the central design decision, from spec §8 — the system must not depend
on an unexplained LLM number.

| Deterministic Python | LLM |
|---|---|
| Hard-constraint checks | Interpreting natural-language intent |
| Every criterion score | Choosing which evidence to gather |
| Weighted total | Writing the explanation |
| Ranking | Phrasing concerns |

Scores are reproducible and unit-testable with no LLM involved. Two identical
inputs always produce an identical score, byte for byte, including the
criterion explanations.

### Performance constraint

Measured on this hardware (Intel Core Ultra 5 125U, CPU-only):
`qwen2.5:3b-instruct` takes ~16 s for a JSON generation and 36–43 s for a
tool-enabled turn once warm; the 7B roughly doubles that. Ranking N candidates
with N LLM calls would take minutes.

Consequence, binding: **no LLM call sits in a request/response path**
(NFR-3.1). The deterministic scorer ranks all candidates instantly and
synchronously; the agent generates explanations only for the results asked
for, asynchronously, and caches them in `match_analyses`. Natural-language
search follows the same shape: `POST /search/describe` enqueues an
`INTERPRET_INTENT` job and redirects to a page that waits for it. The
architecture and the hardware reality agree — this is a further reason the
agent is a polling process rather than an inline call.

---

## 6. RAG and the Vector Database

The Vector DB holds the **curated knowledge base**, never transactional data.
Adopter and animal records live in SQL Server and are reached through MCP
tools.

```
knowledge/*.md                       (17 guides, 104 chunks)
  → chunk on the documents' own `##` headings, splitting any section over
    1400 characters into windows with 160 characters of overlap
  → embed (nomic-embed-text via Ollama, 768-dim)
  → store (ChromaDB, persistent, data/chroma/)
  → retrieve (semantic top-k, filtered by a squared-L2 distance threshold)
  → the agent cites the passages it used
```

Semantic retrieval is required, not keyword matching — blueprint §7.
`tests/agent/test_rag_retrieval.py::test_retrieval_succeeds_with_no_keyword_overlap`
asserts that a query sharing no vocabulary with the target chunk still
retrieves it.

Retrieval failure is not fatal: a stopped Ollama host or a corrupt store logs
a warning and returns no evidence, so an embedding outage costs the prose and
not the analysis.

## 7. Web search policy

Per spec §13, gated rather than automatic:

1. RAG first. **If the curated knowledge base answered, the gate stays shut** —
   even for a question about current or external facts. Sufficiency wins.
2. Web search only when retrieval came back empty.
3. **Never** to retrieve the application's own adopter or animal records.
4. Web content never overrides authoritative application data or business
   rules.
5. Sources are recorded when external information materially affects an
   explanation, and marked `cited` when it does.

Implemented as an explicit gate function, `decide_whether_to_search`, whose
six outcomes are named values — so "did the agent decide correctly *not* to
search?" is a testable question, and the model is told **which rule** refused
it so it can re-plan.

## 8. MCP

Two local tools over stdio, per blueprint §8 and spec §14:
`get_adopter_profile` and `get_animal_profile`. Full detail in `docs/MCP.md`.

The architectural point: **the agent does not own the application's data.** It
requests it through tools, exactly as it would any external service. The tool
descriptions the model reads are the server's own docstrings, read at runtime
through `ClientSession.list_tools()`, so there is no second copy to keep in
step.

## 9. Request lifecycle example

*Staff approve an application — the write the whole design exists to serve:*

```
POST /applications/<application_id>/decide       decision=APPROVE
  → matches.decide_application()                 app/controllers/match_controller.py
      1. validate `decision` against a tuple of four permitted values
      2. @require_sign_in, @require_staff()  — server-side, not a hidden button
      3. bus.dispatch_command(ApproveApplicationCommand(...))
           → ApproveApplicationHandler.handle(command, session)
               · load the application and its animal
               · domain rules: is this transition legal? is the animal still
                 free to promise? (ensure_application_may_be_approved)
               · append ApplicationApproved
               · for each of the adopter's OTHER active applications:
                     append ApplicationClosedDueToOtherApproval(caused_by=A)
                     project status=CLOSED, closed_because_application_id=A
               · move the animal to ADOPTION_IN_PROGRESS, append
                 AnimalStatusChanged
               · write a notification for the adopter
               · return the number of applications closed  (an int, not a DTO)
      4. flash a message built from that count
      5. redirect → GET /animals/<animal_id>/adopters   (a separate Query)

meanwhile, process 2:
  the agent polls analysis_jobs, picks up any queued analysis, runs its loop,
  writes MatchAnalysis and appends AIAnalysisCompleted. The staff member's
  next page load shows the completed explanation.
```

The command returns a count, never the screen's data. The data for the next
screen comes from `RankApplicantsQuery`. That is CQRS doing its job.

A human pressed that button. Nothing the agent produces reaches this path;
the ranking beside the button is advice (rule R4, spec §6.4), and
`tests/unit/test_architecture_guard.py::test_agent_never_names_a_decision_command`
proves the agent cannot even name the command.

## 10. Technology decisions

| Concern | Choice | Why |
|---|---|---|
| Web framework | Flask 3.0 | Required by blueprint §9 |
| Templating | Jinja2, server-rendered | Maps cleanly to the MVC View layer; no Node on this machine |
| ORM | SQLAlchemy 2.0 | Typed, mature, dialect-portable |
| DB driver | pymssql | No system ODBC driver needed. The SQLAlchemy URL sets `charset=CP1252` so FreeTDS on Windows can finish the SQL Server 2014 login (without it the handshake dies as TDS 20002). |
| Cloud DB | Somee.com SQL Server 2014 | Provided; blueprint §11 requires cloud hosting |
| Schema management | `Base.metadata.create_all` via `scripts/db.py` | One database, one developer; a migration history would be unpaid cost. No Alembic. |
| CSRF | Flask-WTF `CSRFProtect` + `SameSite=Lax` | NFR-4.3; a hidden token on every POST form, and a cookie the browser will not attach cross-site |
| LLM | Ollama, `qwen2.5:3b-instruct` | Local, free, no API key, adequate tool calling and JSON |
| Embeddings | nomic-embed-text via Ollama | Reuses the Ollama runtime; avoids a 2.5 GB PyTorch dependency |
| Vector DB | ChromaDB | Lightweight, local, persistent |
| Web search | Tavily | Built for agents; clean ranked results; degrades to an offline stub with no key |
| MCP | Official `mcp` Python SDK | stdio transport as blueprint §8 requires |
| Tests | pytest + Playwright (Python) | Playwright's Python binding needs no Node |
| TLS | truststore | Corporate Palo Alto proxy re-signs HTTPS; Python must use the Windows trust store. Injected at import time in `app/config.py` and again in `agent_service/__main__.py`. |
