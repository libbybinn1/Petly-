# PetMatch — Master Implementation Plan

**Status:** Awaiting answers to the Blocking Questions in §2 before Phase 1 begins.

**Method:** Specification-Driven Agentic Development (SDAD). Document first, then implement, then test, then verify against the spec. No feature starts before its spec section is written.

**Source of truth:** `PetMatch_Final_Project_Specification_EN.docx` (product) + `Final_Project_Specification_and_Requirements_...docx` (course blueprint). Where the two disagree, the course blueprint wins — it states this explicitly in its §1.

---

## 1. Requirement Traceability Matrix

Every mandatory checklist item from course blueprint §20, mapped to where it gets built and how it is proven. Nothing in this table may be dropped.

| # | Mandatory Requirement | Built In | Proven By |
|---|---|---|---|
| 1 | Defined topic, not the HR example | Whole system — animal adoption | `docs/PRD.md` |
| 2 | Authentication | `app/controllers/auth_controller.py` | `tests/integration/test_auth.py` |
| 3 | Two+ roles (Adopter, Staff) | `app/domain/user.py`, `app/security/` | `tests/api/test_authorization.py` |
| 4.1 | Search | Structured + natural-language animal search | `tests/integration/test_search.py`, E2E |
| 4.2 | Details view | Animal details screen | E2E `test_adopter_journey.py` |
| 4.3 | Tabular display | Staff animal + application tables | E2E `test_staff_journey.py` |
| 4.4 | Dashboard | Staff operational dashboard (spec §22) | `tests/integration/test_dashboard_queries.py` |
| 4.5 | Data entry | 7 business forms (spec §21) | `tests/api/test_forms_validation.py` |
| 5 | AI Agent as independent process | `agent_service/` — separate OS process | `tests/agent/`, demo script |
| 6 | Web Search integrated | `agent_service/tools/web_search.py` | `tests/agent/test_web_search_policy.py` |
| 7 | Vector DB + RAG | `agent_service/rag/` + ChromaDB | `tests/agent/test_rag_retrieval.py` |
| 8 | Two+ local MCP tools over stdio | `mcp_server/petmatch_tools.py` | `tests/agent/test_mcp_stdio.py` |
| 9 | Flask | `app/` | all integration tests |
| 10 | MVC | `controllers/` / `views/` / `domain/` separation | `docs/ARCHITECTURE.md` + arch test |
| 11 | CQRS | `app/cqrs/commands/` vs `app/cqrs/queries/` | `tests/unit/test_cqrs_separation.py` |
| 12 | Event Sourcing | `app/eventstore/` append-only + projections | `tests/integration/test_event_sourcing.py` |
| 13 | Cloud database | Managed PostgreSQL — see §2 Q2 | connection smoke test |
| 14 | External skill (sh.skills) | see §2 Q5 | `.claude/skills/` |
| 15 | Self-defined skill | Clean Code, DB Mgmt, Testing skills | `.claude/skills/` |
| 16 | One+ Rule | `CLAUDE.md` + `.claude/rules/` | reviewed in presentation |
| 17 | Organized MD docs | `docs/` — 11 files | `docs/` |
| 18 | GitHub | repo + clear commits | git history |
| 19 | Unit/Integration/E2E/Agent tests | `tests/` | pytest + Playwright reports |
| 20 | Professional UI/UX | Jinja2 + design system | E2E screenshots |

---

## 2. Blocking Questions

These are the decisions from PetMatch spec §30 that I cannot resolve from the documents. Each one materially changes the code I write.

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
- **Animal images:** local `static/uploads/` with the URL in Postgres, seeded with public-domain photos. Spec §24 leaves storage as an implementation decision.
- **MCP tools:** exactly the two the spec names — `get_adopter_profile`, `get_animal_profile`. A third only if a real need appears (spec §14 warns against padding the count).
- **Seed scale:** ~40 animals, ~25 adopters, ~30 applications, ~10 invitations — enough for a believable dashboard.
- **Agent↔app channel:** DB-backed job queue (`analysis_jobs` table) polled by the separate agent process. This keeps the agent genuinely independent rather than an in-process import.

---

## 3. Locked Architecture

```
Browser
  |
  v
Flask App (app/)
  |- controllers/   Blueprints. HTTP only: parse, authorize, dispatch, render. No business logic.
  |- views/         Jinja2 templates + view models.
  |- domain/        Entities, value objects, business rules. No Flask, no SQLAlchemy imports.
  |- cqrs/
  |    |- commands/ State-changing. Emits events, never returns read data.
  |    |- queries/  Read-only. Never mutates. Reads projections.
  |    |- bus.py    Dispatcher; enforces the separation.
  |- eventstore/    Append-only domain_events table + projectors.
  |- repositories/  Data access. The only layer touching SQLAlchemy sessions.
  |- security/      Auth + role enforcement at server level, not hidden buttons.
       |
       v
  Cloud PostgreSQL  -- source of truth for all business data

Agent Service (agent_service/)  -- SEPARATE OS PROCESS
  |- loop.py        Reason -> Act -> Observe -> Re-plan. Not a single LLM call.
  |- rag/           ChromaDB: ingestion, chunking, embedding, semantic retrieval.
  |- tools/         web_search (policy-gated), mcp_client (stdio).
  |- scoring/       Deterministic weighted criteria. Math scores; the LLM explains.
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
- 0.4 `CLAUDE.md` + `.claude/rules/` — architecture, testing and style rules for the coding agent.
- 0.5 venv, `requirements.txt`, `pyproject.toml`, ruff + mypy strict.

**Done when:** `ruff check` and `mypy` run clean on the skeleton.

### Phase 1 — Documentation Set
All 11 files mandated by blueprint §14, before any app code: `PRD.md`, `REQUIREMENTS.md`, `FEATURES.md`, `ARCHITECTURE.md`, `MODEL_DATA.md`, `API.md`, `AGENT.md`, `MCP.md`, `TESTING.md`, `UX.md`, `SKILLS_AND_RULES.md`.

**Done when:** every mandatory requirement in §1 traces to a documented feature.

### Phase 2 — Data Layer and Cloud DB
- 2.1 SQLAlchemy models: User, AdopterProfile, Animal, AnimalImage, Application, Invitation, MatchAnalysis, Notification, DomainEvent, AnalysisJob.
- 2.2 Alembic migrations; constraints, FKs, indexes, enums.
- 2.3 Cloud connection verified; local Docker Postgres for dev.
- 2.4 Seed script with images.

**Done when:** the `db-management` skill can drop, migrate and seed in one command against both local and cloud.

### Phase 3 — Event Store
- 3.1 Append-only `domain_events`: id, type, aggregate_id, aggregate_type, occurred_at, actor_id, payload, version.
- 3.2 Event catalog: `ApplicationSubmitted`, `ApplicationUnderReview`, `ApplicationApproved`, `ApplicationRejected`, `ApplicationWithdrawn`, `ApplicationClosedDueToOtherApproval`, `ApplicationReopened`, `InvitationSent`, `InvitationViewed`, `InvitationAccepted`, `InvitationDeclined`, `InvitationExpired`, `AIAnalysisCompleted`, `AnimalStatusChanged`, `AdopterProfileUpdated`.
- 3.3 Projectors rebuilding current-state tables from the log.
- 3.4 Replay capability + history view.

**Done when:** `test_event_sourcing.py` proves replayed state equals live state, and proves a closed application can be reopened from history.

### Phase 4 — CQRS Skeleton
- 4.1 `Command`/`Query` base types, handler registry, bus.
- 4.2 Architectural test: no query handler opens a write transaction; no command handler returns read DTOs.

**Done when:** `test_cqrs_separation.py` passes.

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
- 11.1 Author ~12 curated knowledge documents: species care, space and activity needs, child compatibility, multi-pet households, senior and special-needs animals, organization adoption policy.
- 11.2 Ingest → chunk → embed → ChromaDB → semantic retrieval.
- 11.3 Prove semantic, not keyword, retrieval.

**Done when:** a query sharing no keywords with the target chunk still retrieves it.

### Phase 12 — Autonomous Agent Process
- 12.1 Independent process polling `analysis_jobs`.
- 12.2 Reason → Act → Observe → Re-plan loop with genuine tool selection.
- 12.3 Web-search policy gate per spec §13: RAG first; web only when genuinely needed; never for the app's own records; never overriding authoritative data.
- 12.4 Structured JSON output: score, reasons, concerns, missing information, evidence and sources.
- 12.5 Natural-language intent parsing (spec §6.3) and Profile+Intent fusion (spec §6.4).
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
- 15.2 All nine demo scenarios from spec §28 scripted and passing.
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
| `feature-spec` | Enforce the blueprint §16 feature template before any feature is coded. |
| `arch-guard` | Verify MVC and CQRS boundaries are not violated by a change. |

## 6. Rules To Define

- **R1 Ambiguity Rule** — do not begin implementation while a major requirement is ambiguous; stop and ask. Blueprint §15 names this explicitly as a recommended rule.
- **R2 Architecture Rule** — controllers hold no business logic; domain imports no framework; only repositories touch the session; commands never return read data.
- **R3 Testing Rule** — no feature is done without unit and integration coverage plus at least one negative test.
- **R4 Agent Rule** — the agent never makes the final adoption decision, never invents facts absent from its sources, and always records which sources it used.
- **R5 Documentation Rule** — code and `docs/` may never contradict; a feature change updates both in the same commit.

## 7. Definition of Done

Per blueprint §18: requirement defined → user story and acceptance criteria → architecture fits → implemented → tests written and passing → UI/API matches spec → documentation updated → no doc/code contradiction → verified in a real run.
