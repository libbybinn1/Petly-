# PetMatch — Master Implementation Plan

**Status:** Complete. All blocking questions in §2 are resolved — see the answers recorded there — and every phase in §0 is done, including the hardening sprint in §0.1.

**Method:** Specification-Driven Agentic Development (SDAD). Document first, then implement, then test, then verify against the spec. No feature starts before its spec section is written.

**Source of truth:** `PetMatch_Final_Project_Specification_EN.docx` (product) + `Final_Project_Specification_and_Requirements_...docx` (course blueprint). Where the two disagree, the course blueprint wins — it states this explicitly in its §1.

---

## 0. Progress

Updated as phases complete, per rule R5.

| Phase | Status |
|---|---|
| 0 Foundations, skills, rules | Done |
| 1 Documentation set (11 files) | Done |
| 2 Data layer and cloud DB | Done |
| 3 Event store | Done |
| 4 CQRS skeleton | Done |
| 5 Auth and authorization | Done |
| 6 Animals and adopter profiles | Done |
| 7 Search, details, tables | Done |
| 8 Applications and invitations | Done, including the spec 7.5 cascade and 72-hour window |
| 9 Deterministic matching engine | Done |
| 10 MCP server over stdio | Done |
| 11 RAG pipeline | Done |
| 12 Autonomous agent process | Done |
| 13 AI-facing screens | Done |
| 14 Dashboard | Done |
| 15 E2E, hardening, demo | Done - API and E2E suites written; `scripts/verify_requirements.py` inspects 23 mandatory items |
| 16 Hardening sprint, 2026-09-23/24 | Done - see below |

All sixteen phases complete. `scripts/verify_requirements.py` reports 23/23.
It verifies by *inspecting behaviour* - building the Flask app, parsing the
code with `ast`, calling pure functions - rather than by grepping for
strings, which is why its earlier "25/25" was worth less than today's 23.

Known operational note: the end-to-end suite runs a real browser against a
real server, but against a **seeded local SQLite file**, not the cloud
database. Somee's free tier throttles under a browser page's request
fan-out, and a suite that timed out on rate limits was testing the hosting
tier rather than the journeys. `tests/e2e/conftest.py` says so in its own
docstring.

---

## 0.1 Hardening sprint — 2026-09-23/24

A full requirements audit against the course blueprint and the product spec
found thirteen blocking gaps. All of them were closed in a two-day sprint by
a team of agents working in parallel. What changed:

| Work | Outcome |
|---|---|
| **Requirements audit** | Every blueprint 20 checklist item and every spec section re-verified by inspection; the findings drove everything below. |
| **Seed data** | `scripts/seed_roster.py`, `scripts/seed_people.py`, `scripts/seed_history.py`: 157 animals across eleven kinds, 40 adopters (3 deliberately incomplete), 3 staff, 81 applications, 32 invitations covering every status, 48 notifications, 40 match analyses, 263 domain events. Every dashboard tile is non-zero. |
| **Agent reason-act loop** | `agent_service/loop.py` now drives a real `while` loop with a tool manifest built from the live MCP server; the model chooses the tool. `reasoning_session.py`, `explanation.py` and the grounding check are new. |
| **Staff decision route** | `POST /applications/<id>/decide` with APPROVE, REJECT, REVIEW and REVERSE. The spec 7.5 cascade was previously reachable only from a test. |
| **Animal lifecycle** | `GET/POST /animals/new`, `GET/POST /animals/<id>/edit`, `POST /animals/<id>/status`, `app/domain/animal_rules.py`, and the mandatory-image rule enforced on both write paths. |
| **CSRF** | `CSRFProtect` on every POST form, `SESSION_COOKIE_SAMESITE=Lax`, `HttpOnly`, and a friendly 400 page for an expired form. |
| **Notification inbox** | `GET /my/notifications` plus mark-read routes; notifications were written but unreadable. |
| **Event replay** | `app/eventstore/projections.py`: `replay_application`, `replay_invitation`, `rebuild_projections`. FR-13.3 went from claimed to proven. |
| **Asynchronous intent** | `POST /search/describe` enqueues an `INTERPRET_INTENT` job and redirects; the synchronous model call that sat in a request path is gone (NFR-3.1). |
| **Design system** | One token set, contrast fixed, dark mode completed, shared partials for the score ring, criterion bars and the pending block. |
| **Verification** | `scripts/verify_requirements.py` rewritten to inspect behaviour, `tests/unit/test_architecture_guard.py` enforces CLAUDE.md R2 mechanically, and `tests/unit/test_docs_reference_real_artifacts.py` fails the build on a documentation claim that names something that does not exist. |

---

## 1. Requirement Traceability Matrix

Every mandatory checklist item from course blueprint §20, mapped to where it gets built and how it is proven. Nothing in this table may be dropped.

| # | Mandatory Requirement | Built In | Proven By |
|---|---|---|---|
| 1 | Defined topic, not the HR example | Whole system — animal adoption | `docs/PRD.md` |
| 2 | Authentication | `app/controllers/auth_controller.py`, `app/cqrs/queries/auth_queries.py` | `tests/api/test_authorization.py`, `tests/api/test_feature_auth_boundary.py` |
| 3 | Two+ roles (Adopter, Staff) | `app/domain/enums.py` (`UserRole`), `app/security/authorization.py` | `tests/api/test_qa_authorization.py` |
| 4.1 | Search | Structured filters plus asynchronous natural-language search | `tests/api/test_forms_and_validation.py`, `tests/api/test_feature_natural_language_search.py`, `tests/api/test_feature_age_filter.py` |
| 4.2 | Details view | Animal details screen | `tests/e2e/test_journeys.py` |
| 4.3 | Tabular display | `app/templates/animals/manage.html` | `tests/e2e/test_accessibility.py`, `tests/e2e/test_journeys.py` |
| 4.4 | Dashboard | Staff operational dashboard (spec §22) | `tests/api/test_feature_dashboard_and_history.py` |
| 4.5 | Data entry | 7 business forms (spec §21) | `tests/api/test_forms_and_validation.py`, `tests/api/test_feature_animal_lifecycle.py`, `tests/api/test_feature_staff_decision.py` |
| 5 | AI Agent as independent process | `agent_service/` — separate OS process | `tests/agent/test_reasoning_loop.py`, `tests/agent/test_worker_jobs.py` |
| 6 | Web Search integrated | `agent_service/tools/web_search.py` | `tests/agent/test_agent_loop.py`, `tests/agent/test_reasoning_loop.py` |
| 7 | Vector DB + RAG | `agent_service/rag/` + ChromaDB | `tests/agent/test_rag_retrieval.py` |
| 8 | Two+ local MCP tools over stdio | `mcp_server/server.py` | `tests/agent/test_mcp_stdio.py` |
| 9 | Flask | `app/` | `tests/api/` |
| 10 | MVC | `controllers/` / `templates/` / `domain/` separation | `tests/unit/test_architecture_guard.py` |
| 11 | CQRS | `app/cqrs/commands/` vs `app/cqrs/queries/` | `tests/unit/test_architecture_guard.py` |
| 12 | Event Sourcing | `app/eventstore/store.py` append-only + `app/eventstore/projections.py` | `tests/integration/test_event_sourcing.py`, `tests/integration/test_qa_event_store.py` |
| 13 | Cloud database | Somee.com **SQL Server 2014** — see §2 Q2 | `scripts/check_environment.py` |
| 14 | External skill (sh.skills) | `.claude/skills/mcp-builder`, `.claude/skills/webapp-testing` | `scripts/verify_requirements.py` |
| 15 | Self-defined skill | `.claude/skills/clean-code`, `.claude/skills/db-management`, `.claude/skills/testing` | `scripts/verify_requirements.py` |
| 16 | One+ Rule | `CLAUDE.md` — five rules, R1–R5 | `docs/SKILLS_AND_RULES.md`, `tests/unit/test_architecture_guard.py` |
| 17 | Organized MD docs | `docs/` — 11 files | `tests/unit/test_docs_reference_real_artifacts.py` |
| 18 | GitHub | repo + clear commits | git history |
| 19 | Unit/Integration/E2E/Agent tests | `tests/` | 910 collected tests; see `docs/TESTING.md` §3 |
| 20 | Professional UI/UX | Jinja2 + one token set | `tests/e2e/test_accessibility.py` |

---

## 2. Blocking Questions — all resolved

These were the decisions from PetMatch spec §30 that could not be resolved from
the documents. Recorded here with their answers, because two of them changed the
architecture substantially.

| # | Question | Answer |
|---|---|---|
| Q1 | LLM provider | Local Ollama, `qwen2.5:3b-instruct`. The 7B is blocked at 95% by the corporate proxy; swapping it in is one `.env` line. |
| Q2 | Cloud database | **Somee.com, MS SQL Server 2014** — the spec's "SOMY.com". Not PostgreSQL, which changed the dialect, the JSON strategy and the key strategy. |
| Q3 | Web search | Tavily. Needed `truststore` to work behind the corporate TLS proxy. |
| Q4 | Embeddings | `nomic-embed-text` via Ollama — one runtime, no PyTorch. |
| Q5 | sh.skills | `mcp-builder` and `webapp-testing`, installed from the ecosystem's GitHub source while skills.sh itself was returning 503. |
| Q6 | GitHub | A remote is configured on the local repository. The push itself is still pending the user's sign-in, so the history exists but is not yet on GitHub. |

The original questions follow, kept because they record *why* each decision
was open rather than assumed.

### Q1 — LLM provider and credentials
The Agent needs a real LLM. No API key is present in this environment. Which do you have: OpenAI, Anthropic, Azure OpenAI, Google Gemini, a course-provided endpoint, or a local model via Ollama? I will build behind an `LLMProvider` interface either way, with a deterministic offline stub so the whole test suite runs without keys — but I need to know which real adapter to wire and demo.

### Q2 — Cloud database
Spec §17 names **SOMY.com**. I searched and could not verify that this provider exists; the spec itself marks it unconfirmed ("subject to confirming that it provides the required relational database capabilities"). Separately, blueprint §11 makes a *cloud-hosted* DB mandatory, which tensions against your instruction to build "entirely locally."

My proposal: SQLAlchemy + PostgreSQL, on local Docker Postgres for development, pointed at a free managed Postgres via `DATABASE_URL` to satisfy the cloud requirement. Do you (a) have real SOMY.com credentials, (b) want Neon or Supabase, or (c) have a course-provided database?

### Q3 — Web Search provider
Requires a key. Tavily, Brave Search, Serper, or Google CSE? Or should the agent call an MCP web-search server you already have configured? A stub suffices for tests, but the demo needs a live one.

### Q4 — Embeddings for the Vector DB
Two options with different trade-offs: local `sentence-transformers` (no key, fully offline, ~90MB one-time model download) or provider embeddings (needs the Q1 key, no download). **Local is my recommendation** — it keeps the RAG pipeline reproducible, free, and independent of Q1.

### Q5 — The "sh.skills" external skill
Blueprint §15 requires at least one skill from `sh.skills`. I do not know that marketplace. Is it a specific plugin marketplace you were given a URL for, or does the bundled Anthropic skill set available in this session count? This is a graded checklist item, so I will not guess.

### Q6 — GitHub
Blueprint §19 mandates GitHub. Local `git init` only, or also create and push a remote? If remote, is `gh` authenticated under your account?

### Non-blocking defaults — tell me only if you disagree
- **UI language:** English, LTR.
- **Frontend:** Flask + Jinja2, server-rendered. No Node is installed here, and server-rendered views map cleanly onto the MVC "View" layer the blueprint demands.
- **E2E:** Playwright for **Python** (`pytest-playwright`) — works without Node.
- **Animal images:** local `app/static/uploads/` with only the URL stored in SQL Server, seeded with freely licensed photographs. Spec §24 leaves storage as an implementation decision, and the free tier's size cap settles it.
- **MCP tools:** exactly the two the spec names — `get_adopter_profile`, `get_animal_profile`. A third only if a real need appears (spec §14 warns against padding the count).
- **Seed scale:** 157 animals across eleven kinds (dogs, cats, rabbits, guinea pigs, hamsters, birds, reptiles, amphibians, exotic mammals, farm animals, poultry), 40 adopters — three of them deliberately incomplete — 3 staff, ~79 applications including one approval with its §7.5 cascade, 32 invitations covering every status, and a set of deterministic match analyses. Sized so every dashboard tile is non-zero rather than merely so the pages are not empty. The roster lives in `scripts/seed_roster.py` and the people in `scripts/seed_people.py`; `knowledge/` holds 17 curated guides.
- **Agent↔app channel:** DB-backed job queue (`analysis_jobs` table) polled by the separate agent process. This keeps the agent genuinely independent rather than an in-process import.

---

## 3. Locked Architecture

```
Browser
  |
  v
Flask App (app/)
  |- controllers/   Blueprints. HTTP only: parse, authorize, dispatch, render. No business logic.
  |- templates/     Jinja2 views. View models live beside their query.
  |- domain/        Entities, value objects, business rules. No Flask, no SQLAlchemy imports.
  |- cqrs/
  |    |- base.py    Command, Query, MessageBus. Commands commit; queries roll back.
  |    |- commands/  State-changing. Emits events, returns an id, a count or nothing.
  |    |- queries/   Read-only. Never mutates. Reads projections. Holds the view models.
  |- eventstore/    store.py (append-only domain_events) + projections.py (replay).
  |- infrastructure/ SQLAlchemy models, engine and session factory.
  |- security/      Auth + role enforcement at server level, not hidden buttons.
       |
       v
  Somee.com SQL Server 2014  -- source of truth for all business data

Agent Service (agent_service/)  -- SEPARATE OS PROCESS
  |- worker.py      The process: claim a job, dispatch it, persist, cache.
  |- loop.py        Reason -> Act -> Observe -> Re-plan. Not a single LLM call.
  |- rag/           ChromaDB: ingestion, chunking, embedding, semantic retrieval.
  |- tools/         web_search (policy-gated), mcp_tools (stdio client).
                    Scoring is NOT here: it is app/domain/matching.py, shared
                    with the web tier so one arithmetic serves both.
       | stdio (MCP protocol)
       v
MCP Server (mcp_server/)  -- SEPARATE PROCESS
  |- get_adopter_profile
  |- get_animal_profile
```

### Why Event Sourcing here
Blueprint §10 demands this justification. PetMatch spec §7.5 requires that approving one application closes the adopter's other active applications — but *not* deletes them, because a later cancellation or withdrawal may need to reopen them. A current-state-only model destroys the information needed to do that correctly. The event log records *how* each application reached its status, so reopening becomes a replay decision rather than a guess.

### Why CQRS here
The system is heavily read-skewed (search, dashboard, rankings, tables) with a small set of sharply-defined writes (submit application, send invitation, accept/decline, approve, update animal status). Read models can be shaped per screen while writes stay guarded by business rules.

---

## 4. Phased Build

Each phase ends with green tests and a commit. No phase starts before the previous is verified. Every feature gets its `docs/FEATURES.md` section **before** its code.

### Phase 0 — Answers and Foundations
- 0.1 Resolve the §2 blocking questions.
- 0.2 `git init`, `.gitignore`, `README.md`.
- 0.3 `.claude/skills/` — Clean Code, DB Management, Testing skills, defined *before* coding per your directive #5.
- 0.4 `CLAUDE.md` — architecture, testing, documentation, agent and ambiguity rules for the coding agent. They live in one file the agent reads automatically; a separate rules directory under `.claude/` was planned and never needed.
- 0.5 venv, `requirements.txt`, `pyproject.toml`, ruff + mypy strict.

**Done when:** `ruff check` and `mypy` run clean on the skeleton.

### Phase 1 — Documentation Set
All 11 files mandated by blueprint §14, before any app code: `PRD.md`, `REQUIREMENTS.md`, `FEATURES.md`, `ARCHITECTURE.md`, `MODEL_DATA.md`, `API.md`, `AGENT.md`, `MCP.md`, `TESTING.md`, `UX.md`, `SKILLS_AND_RULES.md`.

**Done when:** every mandatory requirement in §1 traces to a documented feature.

### Phase 2 — Data Layer and Cloud DB
- 2.1 SQLAlchemy models: User, AdopterProfile, Animal, AnimalImage, Application, Invitation, MatchAnalysis, Notification, DomainEvent, AnalysisJob.
- 2.2 Constraints, foreign keys, indexes and CHECK-enforced enums declared on the metadata. **Alembic was dropped:** the schema is created with `Base.metadata.create_all` through `scripts/db.py`. One developer, one database and a `fresh` command that rebuilds it in a minute makes a migration history a cost with no payer. `requirements.txt` still pins Alembic; that pin is vestigial.
- 2.3 Cloud connection verified by `scripts/check_environment.py`; `LOCAL_DATABASE_URL` points at SQLite for offline work.
- 2.4 Seed script with images.

**Done when:** `scripts/db.py fresh` can drop, create and seed in one command against both local and cloud.

### Phase 3 — Event Store
- 3.1 Append-only `domain_events`: id, type, aggregate_id, aggregate_type, occurred_at, actor_id, payload, version.
- 3.2 Event catalog: `ApplicationSubmitted`, `ApplicationUnderReview`, `ApplicationApproved`, `ApplicationRejected`, `ApplicationWithdrawn`, `ApplicationClosedDueToOtherApproval`, `ApplicationReopened`, `InvitationSent`, `InvitationViewed`, `InvitationAccepted`, `InvitationDeclined`, `InvitationExpired`, `AIAnalysisCompleted`, `AnimalStatusChanged`, `AdopterProfileUpdated`.
- 3.3 Projectors rebuilding current-state tables from the log.
- 3.4 Replay capability + history view.

**Done when:** `test_event_sourcing.py` proves replayed state equals live state, and proves a closed application can be reopened from history.

### Phase 4 — CQRS Skeleton
- 4.1 `Command`/`Query` base types, handler registry, bus.
- 4.2 Architectural test: no query handler opens a write transaction; no command handler returns read DTOs.

**Done when:** the architectural test passes. It was planned as a file called `test_cqrs_separation.py` and shipped, in the hardening sprint, as `tests/unit/test_architecture_guard.py` — wider than planned, because it enforces every row of CLAUDE.md R2 rather than the CQRS split alone, and because each rule also has a test proving the rule itself catches a synthetic violation.

### Phase 5 — Auth and Authorization
- 5.1 Registration, login, logout, password hashing, sessions.
- 5.2 Role enforcement at controller **and** service level.
- 5.3 Negative tests: an adopter hitting a staff endpoint gets 403 even via a forged form post.

**Done when:** `test_authorization.py` covers every endpoint against every role.

### Phase 6 — Animals and Adopter Profiles
- 6.1 Animal create/edit/status forms, image upload, validation at both layers.
- 6.2 Adopter profile form including the `open_to_proactive_suggestions` opt-in — spec §5.1 makes this required for Find More Adopters.

**Done when:** server-side validation rejects everything the UI rejects.

### Phase 7 — Search, Details, Tables
- 7.1 Structured filter search: species, age, size, activity, children, other animals, location.
- 7.2 Result to details navigation, with the mandatory image.
- 7.3 Staff tabular management screen.

**Done when:** E2E walks search to result to details.

### Phase 8 — Applications and Invitations State Machines
- 8.1 Application lifecycle per spec §25: Submitted → Under Review → Approved / Rejected / Withdrawn / Closed.
- 8.2 Invitation lifecycle: Sent → Viewed → Accepted / Declined / Expired, with **72-hour** expiry.
- 8.3 The §7.5 rule: approving one application closes the adopter's other active ones; history preserved; reopen supported.
- 8.4 Internal notification inbox (spec §23).

**Done when:** unit tests cover every legal and illegal transition.

### Phase 9 — Deterministic Matching Engine
- 9.1 Hard constraints vs soft preferences, made explicit (spec §8).
- 9.2 Two direction-specific weight sets: Adopter→Animal and Animal→Adopter. Spec §9 states these deliberately differ.
- 9.3 Eligibility filter running *before* the agent (spec §10).

**Done when:** scoring is pure, typed, fully unit-tested, and identical for identical input. The math is deterministic; only the explanation is generated.

### Phase 10 — MCP Server over stdio
- 10.1 `get_adopter_profile`, `get_animal_profile`, with LLM-facing docstrings.
- 10.2 stdio transport, launched as a subprocess by the agent.

**Done when:** `test_mcp_stdio.py` spawns the real server over stdio and round-trips both tools.

### Phase 11 — RAG Pipeline
- 11.1 Author the curated knowledge documents: species care, space and activity needs, child compatibility, multi-pet households, senior and special-needs animals, organization adoption policy. **17 guides shipped**, producing 104 chunks.
- 11.2 Ingest → chunk → embed → ChromaDB → semantic retrieval.
- 11.3 Prove semantic, not keyword, retrieval.

**Done when:** a query sharing no keywords with the target chunk still retrieves it.

### Phase 12 — Autonomous Agent Process
- 12.1 Independent process polling `analysis_jobs`.
- 12.2 Reason → Act → Observe → Re-plan loop with genuine tool selection. Delivered in the hardening sprint (§0.1): `agent_service/loop.py` sends a tool manifest built from the live MCP server on every turn and dispatches whichever tool the model names.
- 12.3 Web-search policy gate per spec §13: RAG first; web only when genuinely needed; never for the app's own records; never overriding authoritative data.
- 12.4 Structured JSON output: score, reasons, concerns, missing information, evidence and sources.
- 12.5 Natural-language intent parsing (spec §6.3) and Profile+Intent fusion (spec §6.4). Both delivered in the hardening sprint: the interpretation runs as an `INTERPRET_INTENT` job, and `FindMyPetWithIntentQuery` scores the animals the intent narrowed against the adopter's stored profile.
- 12.6 Persist `MatchAnalysis`, emit `AIAnalysisCompleted`.

**Done when:** agent tests cover structured output, tool selection, RAG use, web-search gating, and failure paths — LLM down, tool error, malformed JSON.

### Phase 13 — AI-Facing Screens
- 13.1 Find My Pet, natural-language search, My Matches.
- 13.2 Find My Adopter (rank existing applicants), Find More Adopters (opted-in discovery, top N).
- 13.3 Match Analysis view with reasons, concerns and sources.
- 13.4 Send invitation flow.

**Done when:** both spec §9 workflows are demonstrable end to end.

### Phase 14 — Dashboard
Available animals, pending applications, applications needing attention, open invitations, expired invitations, animals with no suitable applicants, animals with no applicants at all, recent activity drawn from the event log, and a **Needs Attention** section linking statistics to actions (spec §22).

### Phase 15 — E2E, Hardening, Demo
- 15.1 Playwright suites: adopter journey, staff journey.
- 15.2 All nine demo scenarios from spec §28 walkable by hand; `docs/DEMO.md` is the runbook. The E2E suite covers the browsable subset (public browsing, the adopter journey, the staff journey, authorization and interface quality); the approval cascade and the agent loop are proven by `tests/integration/test_event_sourcing.py` and `tests/agent/`, which do not need a browser.
- 15.3 Coverage report, negative-path sweep, UI/UX polish.
- 15.4 Final doc/code consistency audit against §1 and blueprint §20.

---

## 5. Skills To Create

Per your directive #5. Defined in Phase 0, before application code.

| Skill | Purpose |
|---|---|
| `clean-code` | SRP, descriptive names without abbreviations, early returns, no deep nesting, extracted helpers, strict typing, comprehensive docstrings. Applied to every file written. |
| `db-management` | One command to connect, migrate, reset, seed, and switch local ↔ cloud. |
| `testing` | Trigger specific pytest and Playwright suites with output formatted for fast debugging. |
| ~~`feature-spec`~~ | **Never built.** The blueprint §16 template is enforced by review against `docs/FEATURES.md` instead. |
| ~~`arch-guard`~~ | **Never built as a skill.** The idea was better served as a test: `tests/unit/test_architecture_guard.py` enforces every row of CLAUDE.md R2 mechanically, and `scripts/verify_requirements.py` repeats the checks from the command line. |

## 6. Rules To Define

- **R1 Ambiguity Rule** — do not begin implementation while a major requirement is ambiguous; stop and ask. Blueprint §15 names this explicitly as a recommended rule.
- **R2 Architecture Rule** — controllers hold no business logic and never touch a session; the domain imports no framework; commands never return read data; queries never write; the agent never imports the web tier. The session is held by the CQRS handlers, through the bus.
- **R3 Testing Rule** — no feature is done without unit and integration coverage plus at least one negative test.
- **R4 Agent Rule** — the agent never makes the final adoption decision, never invents facts absent from its sources, and always records which sources it used.
- **R5 Documentation Rule** — code and `docs/` may never contradict; a feature change updates both in the same commit.

## 7. Definition of Done

Per blueprint §18: requirement defined → user story and acceptance criteria → architecture fits → implemented → tests written and passing → UI/API matches spec → documentation updated → no doc/code contradiction → verified in a real run.
