# ARCHITECTURE — PetMatch

**MVC, CQRS, Event Sourcing, the Agent, MCP, and system boundaries.**
Sources: course blueprint §9, §10, §11; PetMatch spec §11, §18, §19.

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
                         │  controllers/  (C)      │
                         │  views/        (V)      │
                         │  domain/       (M)      │
                         │  cqrs/  commands|queries│
                         │  eventstore/            │
                         │  repositories/          │
                         │  security/              │
                         └────────────┬────────────┘
                                      │ SQLAlchemy / pymssql
                         ┌────────────▼────────────┐
                         │  Somee.com SQL Server   │
                         │  (cloud, source of      │
                         │   truth for business    │
                         │   data + event log)     │
                         └────────────┬────────────┘
                                      │ analysis_jobs table (queue)
                                      │ polled, not called
                         ┌────────────▼────────────┐
                         │     AGENT SERVICE       │   process 2
                         │                         │
                         │  loop.py  reason/act    │
                         │  scoring/ deterministic │
                         │  rag/     ChromaDB      │
                         │  tools/   web search    │
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
with its own lifecycle, started by `python -m agent_service`. The MCP server is
a third process that the agent spawns and talks to over stdin/stdout.

---

## 2. MVC

Blueprint §9.1 requires clearly defined Controller / View / Model
responsibilities with no mixing.

### Model — `app/domain/` and `app/repositories/`

`domain/` holds entities, value objects, enums and business rules as plain
Python. **It imports no framework.** This is deliberate and testable: a domain
unit test runs with no Flask application context and no database.

`repositories/` is the only layer permitted to hold a SQLAlchemy session. It
translates between persistence rows and domain objects.

### View — `app/views/`

Jinja2 templates plus the view-model dataclasses passed into them. Templates
**display**; they never decide. A template contains no business conditional —
if a rule determines whether something shows, that rule is evaluated in the
domain or query layer and arrives as a boolean on the view model.

### Controller — `app/controllers/`

Flask blueprints. A controller does exactly four things, in order:

1. Parse and validate the HTTP request into a typed command or query.
2. Check authorization.
3. Dispatch it on the bus.
4. Render a template or return a response.

Any `if` expressing a *business* rule in a controller is a bug.

---

## 3. CQRS

Blueprint §9.2 requires conceptual **and implementation** separation between
operations that modify information and operations that read it.

```
app/cqrs/
├── bus.py                 dispatcher; enforces the split
├── commands/
│   ├── base.py            Command, CommandHandler[T]
│   ├── submit_application.py
│   ├── send_invitation.py
│   ├── respond_to_invitation.py
│   ├── approve_application.py
│   └── update_animal_status.py
└── queries/
    ├── base.py            Query, QueryHandler[T]
    ├── search_animals.py
    ├── get_animal_details.py
    ├── rank_applicants.py
    ├── find_more_adopters.py
    └── dashboard_summary.py
```

### The contract

| | Command | Query |
|---|---|---|
| Purpose | change state | read state |
| Returns | an identifier, or nothing | a read DTO |
| Side effects | appends domain events | none |
| Transaction | write | read-only |
| Idempotent | no | yes |

**A command never returns read data.** When a screen needs data after a write,
the controller dispatches a query afterwards. This is enforced by
`tests/unit/test_cqrs_separation.py`, which inspects handler signatures and
fails the build if a query handler opens a write transaction or a command
handler returns a read DTO.

### Why CQRS suits this system

PetMatch is heavily read-skewed. Search, dashboard, applicant rankings and
tabular listings all read, and each wants differently-shaped data. Writes are
few and sharply defined: submit an application, send an invitation, respond to
one, approve an application, change an animal's status. Each write carries real
business rules; each read wants a denormalized projection. Forcing both through
one model would compromise both.

---

## 4. Event Sourcing

Blueprint §10 requires event sourcing with a documented justification.

### Why event sourcing is right for *this* process

Spec §7.5 states the rule that makes it necessary:

> If one application is approved, the other applications should be closed as no
> longer active... However, the system should preserve history. If the approved
> adoption is later cancelled or the adopter withdraws before the process is
> finalized, the previous applications should not be silently deleted. Their
> historical state remains available, and business rules can allow appropriate
> applications to be reopened.

Consider an adopter with three active applications. One is approved; the other
two close. Two weeks later the adoption falls through.

**With current-state storage only**, those two applications are rows reading
`status = CLOSED`. Nothing records *why* they closed. Were they closed because
another application was approved — in which case reopening is correct — or
because the adopter withdrew, or staff rejected them? The information needed to
decide has been overwritten. Reopening becomes a guess.

**With an event log**, each carries
`ApplicationClosedDueToOtherApproval(caused_by=application_id)`. When that
approval is reversed, the system knows exactly which applications closed because
of it and can reopen precisely those. Reopening becomes a derivation.

That is the justification: the business rule requires knowing *how* a state was
reached, not merely what it is.

### Implementation

Append-only table, never updated or deleted:

| Column | Type | Purpose |
|---|---|---|
| `event_id` | UNIQUEIDENTIFIER | Primary key |
| `event_type` | NVARCHAR(100) | e.g. `ApplicationApproved` |
| `aggregate_type` | NVARCHAR(50) | `Application`, `Invitation`, `Animal` |
| `aggregate_id` | UNIQUEIDENTIFIER | Which instance |
| `sequence_number` | INT | Order within the aggregate; optimistic concurrency |
| `occurred_at` | DATETIME2 | Timezone-aware UTC |
| `actor_user_id` | UNIQUEIDENTIFIER | Who caused it (context/actor) |
| `payload` | NVARCHAR(MAX) | JSON-serialized event data |

This satisfies blueprint §10's minimum of identifier, type, time,
context/actor and data.

> **SQL Server 2014 constraint:** no native JSON type — that arrived in 2016.
> `payload` is `NVARCHAR(MAX)` serialized in Python. No `JSON_VALUE` or
> `OPENJSON` may be used anywhere.

### Event catalog

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
| `AIAnalysisCompleted` | Application / MatchAnalysis |
| `AnimalStatusChanged` | Animal |
| `AdopterProfileUpdated` | AdopterProfile |

### Projections

Current-state tables are maintained alongside the log for efficient querying —
spec §18 explicitly permits this. Projectors apply each event to the read model
as it is appended. `rebuild_projections()` can reconstruct every current-state
table by replaying the log from zero, and
`tests/integration/test_event_sourcing.py` asserts that replayed state equals
live state.

---

## 5. The Agent

Spec §11 and blueprint §6 require a real agent — planning, tool selection,
observation and re-planning — not a single LLM call.

### Independence

The agent is `process 2`. It never imports `app.*`. It receives work by polling
the `analysis_jobs` table and obtains domain data through MCP tools. This is
enforced by R2 in `CLAUDE.md` and is why the Flask app stays responsive while
analysis runs.

### The loop

```
receive job from analysis_jobs
  ↓
plan: what do I need to answer this?
  ↓
┌─→ act: choose a tool
│     · get_adopter_profile   (MCP, stdio)
│     · get_animal_profile    (MCP, stdio)
│     · rag_search            (ChromaDB, local)
│     · web_search            (Tavily, only if gated check passes)
│   ↓
│   observe: incorporate the result
│   ↓
└── re-plan: enough information? if not, loop (max 8 steps)
  ↓
apply deterministic eligibility rules
  ↓
compute deterministic weighted score
  ↓
generate explanation grounded in gathered evidence
  ↓
write MatchAnalysis + append AIAnalysisCompleted
```

### Division of labour: math vs language

This is the central design decision, from spec §8 — the system must not depend
on an unexplained LLM number.

| Deterministic Python | LLM |
|---|---|
| Hard-constraint checks | Interpreting natural-language intent |
| Every criterion score | Writing the explanation |
| Weighted total | Phrasing concerns |
| Ranking | Identifying what information is missing |

Scores are therefore reproducible and unit-testable with no LLM involved. Two
identical inputs always produce an identical score.

### Performance constraint

Measured on this hardware (Intel Core Ultra 5 125U, CPU-only):
`qwen2.5:3b-instruct` takes ~16 s for a JSON generation and ~11 s for a tool
call; the 7B roughly doubles that. Ranking N candidates with N LLM calls would
take minutes.

Consequence, binding: **no LLM call sits in a request/response path.** The
deterministic scorer ranks all candidates instantly and synchronously; the agent
generates explanations only for top results, asynchronously, and caches them in
`MatchAnalysis`. The architecture and the hardware reality agree — this is a
further reason the agent is a polling process rather than an inline call.

---

## 6. RAG and the Vector Database

The Vector DB holds the **curated knowledge base**, never transactional data.
Adopter and animal records live in SQL Server and are reached through MCP tools.

```
docs/knowledge/*.md
  → chunk (headed sections, ~400 tokens, 50-token overlap)
  → embed (nomic-embed-text via Ollama, 768-dim)
  → store (ChromaDB, persistent, data/chroma/)
  → retrieve (semantic top-k with score threshold)
  → agent cites retrieved chunks as evidence
```

Semantic retrieval is required, not keyword matching — blueprint §7. A test
asserts that a query sharing no keywords with the target chunk still retrieves
it.

## 7. Web Search policy

Per spec §13, gated rather than automatic:

1. RAG first for stable domain knowledge the project curates.
2. Web search only when information is external, current, missing from the
   knowledge base, or needs verification.
3. **Never** to retrieve the application's own adopter or animal records.
4. Web content never overrides authoritative application data or business rules.
5. Sources are recorded when external information materially affects an
   explanation.

Implemented as an explicit gate function, unit-tested, so "did the agent decide
correctly *not* to search?" is a testable question.

## 8. MCP

Two local tools over stdio, per blueprint §8 and spec §14:
`get_adopter_profile` and `get_animal_profile`. Full detail in `MCP.md`.

The architectural point: **the agent does not own the application's data.** It
requests it through tools, exactly as it would any external service. This keeps
the boundary honest and makes the tool layer demonstrable.

## 9. Request lifecycle example

*Adopter submits an application:*

```
POST /animals/<id>/apply
  → ApplicationController.submit()
      1. parse form into SubmitApplicationCommand
      2. @require_role(ADOPTER) — server-side, not a hidden button
      3. bus.dispatch(command)
           → SubmitApplicationHandler
               · load Animal + AdopterProfile via repositories
               · domain rules: animal available? profile complete?
                              no duplicate active application?
               · append ApplicationSubmitted to the event store
               · project into the applications table
               · enqueue an analysis_jobs row
      4. redirect → GET /my/applications  (a separate Query)

meanwhile, process 2:
  agent polls analysis_jobs → picks up the job → runs the loop
    → writes MatchAnalysis, appends AIAnalysisCompleted
    → the adopter's next page load shows the completed analysis
```

Note that the command returns an identifier only. The data for the next screen
comes from a query. That is CQRS doing its job.

## 10. Technology decisions

| Concern | Choice | Why |
|---|---|---|
| Web framework | Flask 3.0 | Required by blueprint §9 |
| Templating | Jinja2, server-rendered | Maps cleanly to the MVC View layer; no Node on this machine |
| ORM | SQLAlchemy 2.0 | Typed, mature, dialect-portable |
| DB driver | pymssql | No system ODBC driver needed |
| Cloud DB | Somee.com SQL Server 2014 | Provided; blueprint §11 requires cloud hosting |
| Migrations | Alembic | Standard SQLAlchemy companion |
| LLM | Ollama, qwen2.5 | Local, free, no API key, strong tool calling and JSON |
| Embeddings | nomic-embed-text via Ollama | Reuses the Ollama runtime; avoids a 2.5 GB PyTorch dependency |
| Vector DB | ChromaDB | Lightweight, local, persistent |
| Web search | Tavily | Built for agents; clean ranked results |
| MCP | Official `mcp` Python SDK | stdio transport as blueprint §8 requires |
| Tests | pytest + Playwright (Python) | Playwright's Python binding needs no Node |
| TLS | truststore | Corporate Palo Alto proxy re-signs HTTPS; Python must use the Windows trust store |
