# REQUIREMENTS — PetMatch

**Functional and non-functional requirements.**
Every requirement carries an identifier so features, tests and documentation
can cite it. `MUST` items are mandatory; `SHOULD` items are strong
recommendations from the specifications.

Sources: course blueprint §4, §12, §13, §17; PetMatch spec §4–§25.

---

## 1. Functional requirements

### FR-1 Authentication and accounts

| Id | Requirement | Priority |
|---|---|---|
| FR-1.1 | A visitor MUST be able to register an adopter account with name, email and password. | MUST |
| FR-1.2 | Passwords MUST be stored hashed, never in plain text. | MUST |
| FR-1.3 | A registered user MUST be able to sign in and out. | MUST |
| FR-1.4 | Sign-in failure MUST NOT reveal whether the email exists. | MUST |
| FR-1.5 | A deactivated account MUST be refused sign-in. | MUST |
| FR-1.6 | Sign-out MUST require a POST, so a third-party page cannot trigger it. | MUST |

### FR-2 Roles and authorization

| Id | Requirement | Priority |
|---|---|---|
| FR-2.1 | The system MUST define exactly two roles: Adopter and Staff. | MUST |
| FR-2.2 | Authorization MUST be enforced server-side on every protected route. Hiding a button is not sufficient (blueprint §12). | MUST |
| FR-2.3 | An adopter MUST receive 403 on any staff route, including direct POSTs. | MUST |
| FR-2.4 | An adopter MUST only be able to read their own profile, applications and invitations. Ownership MUST be checked in the query, not only in the URL. | MUST |
| FR-2.5 | Staff MUST NOT be able to edit an adopter's profile or respond to an invitation on their behalf. | MUST |

### FR-3 Adopter profile

| Id | Requirement | Priority |
|---|---|---|
| FR-3.1 | An adopter MUST be able to record home type, yard, children, other animals, experience, activity level, daily hours available and location. | MUST |
| FR-3.2 | The profile MUST include an explicit opt-in for proactive suggestions (spec §5.1). | MUST |
| FR-3.3 | The system MUST track whether a profile is complete; completeness gates Find More Adopters. | MUST |
| FR-3.4 | Profile updates MUST append an `AdopterProfileUpdated` event. | MUST |

### FR-4 Animals

| Id | Requirement | Priority |
|---|---|---|
| FR-4.1 | Staff MUST be able to create and edit animals. | MUST |
| FR-4.2 | Every animal MUST have at least one image (spec §24). | MUST |
| FR-4.3 | An animal MUST record species, breed, age, size, temperament, activity level, child and animal compatibility, special needs, required space, location and status. | MUST |
| FR-4.4 | Staff MUST be able to change an animal's status; each change appends `AnimalStatusChanged`. | MUST |
| FR-4.5 | Animal status MUST follow the state machine in `MODEL_DATA.md` §3. | MUST |

### FR-5 Search and browsing

| Id | Requirement | Priority |
|---|---|---|
| FR-5.1 | Any visitor MUST be able to search animals by free text over name, breed and description. | MUST |
| FR-5.2 | Search MUST support filters for species, size, activity level, and the two compatibility flags. | MUST |
| FR-5.3 | A search result MUST link to a details view (blueprint §13). | MUST |
| FR-5.4 | Results MUST be paginated. | MUST |
| FR-5.5 | A zero-result search MUST explain how to widen the search rather than showing a bare message. | SHOULD |
| FR-5.6 | An adopter MUST be able to search in natural language (spec §6.3). | MUST |

### FR-6 Tabular display and dashboard

| Id | Requirement | Priority |
|---|---|---|
| FR-6.1 | Staff MUST have a table view of every animal with status and species filters (blueprint 4.3). | MUST |
| FR-6.2 | Staff MUST have a dashboard showing available animals, pending applications, applications needing attention, open invitations, expired invitations, animals with no suitable applicants, animals with no applicants at all, and recent activity (spec §22). | MUST |
| FR-6.3 | The dashboard MUST include a "Needs Attention" section linking figures to actions. | SHOULD |
| FR-6.4 | Recent activity MUST be derived from the event log, not a separate audit table. | MUST |

### FR-7 Applications

| Id | Requirement | Priority |
|---|---|---|
| FR-7.1 | An adopter MUST be able to apply for multiple animals (spec §5.3). | MUST |
| FR-7.2 | An adopter MUST NOT hold two active applications for the same animal. | MUST |
| FR-7.3 | An application MUST only be accepted for an animal whose status is AVAILABLE. | MUST |
| FR-7.4 | An application MUST follow the state machine in `MODEL_DATA.md` §3. | MUST |
| FR-7.5 | Approving an application MUST close the adopter's other active applications, recording `ApplicationClosedDueToOtherApproval` with the causing application id (spec §7.5). | MUST |
| FR-7.6 | Closed applications MUST NOT be deleted; their history MUST remain queryable. | MUST |
| FR-7.7 | If an approval is reversed, exactly those applications closed *because of it* MUST be eligible to reopen. Applications closed for other reasons MUST NOT be affected. | MUST |

### FR-8 Invitations

| Id | Requirement | Priority |
|---|---|---|
| FR-8.1 | Staff MUST be able to send an invitation to an eligible adopter for a specific animal. | MUST |
| FR-8.2 | An invitation MUST expire 72 hours after it is sent (spec §7.4). | MUST |
| FR-8.3 | An adopter MUST be able to accept or decline while the invitation is open. | MUST |
| FR-8.4 | Accepting an invitation MUST NOT approve an adoption. It starts an application. | MUST |
| FR-8.5 | Invitations MUST only be sent to adopters who opted in to proactive suggestions. | MUST |
| FR-8.6 | Every invitation state change MUST append its event. | MUST |

### FR-9 Matching

| Id | Requirement | Priority |
|---|---|---|
| FR-9.1 | The system MUST compute an Adopter→Animal score for Find My Pet. | MUST |
| FR-9.2 | The system MUST compute an Animal→Adopter score for Find My Adopter. | MUST |
| FR-9.3 | The two directions MUST use different weightings (spec §9). | MUST |
| FR-9.4 | Scoring MUST be deterministic: identical input gives identical output, with no LLM involvement (spec §8). | MUST |
| FR-9.5 | Hard constraints MUST disqualify a candidate; soft preferences MUST only reduce the score. | MUST |
| FR-9.6 | A deterministic eligibility filter MUST run before the agent ranks anyone (spec §10). | MUST |
| FR-9.7 | Every match MUST be presented with reasons and concerns, never a bare number. | MUST |

### FR-10 The agent

| Id | Requirement | Priority |
|---|---|---|
| FR-10.1 | The agent MUST run as an independent OS process, not inside the Flask app (blueprint §4.5). | MUST |
| FR-10.2 | The agent MUST obtain domain records through MCP tools over stdio, not by importing the application. | MUST |
| FR-10.3 | The agent MUST consult the Vector DB via RAG as part of its assessment (blueprint §7). | MUST |
| FR-10.4 | The agent MUST use web search only when the curated knowledge base cannot answer (spec §13). | MUST |
| FR-10.5 | The agent MUST NOT use web search to retrieve the application's own records. | MUST |
| FR-10.6 | The agent MUST return structured JSON: score, reasons, concerns, missing information, evidence. | MUST |
| FR-10.7 | The agent MUST NOT make the final adoption decision (spec §6.4). | MUST |
| FR-10.8 | The agent MUST NOT state facts absent from its retrieved sources. | MUST |
| FR-10.9 | Sources materially affecting an explanation MUST be recorded and displayable. | MUST |
| FR-10.10 | A tool or model failure MUST degrade gracefully, not crash the loop. | MUST |

### FR-11 MCP tools

| Id | Requirement | Priority |
|---|---|---|
| FR-11.1 | The project MUST provide at least two local MCP tools communicating over stdio (blueprint §8). | MUST |
| FR-11.2 | The tools MUST be `get_adopter_profile` and `get_animal_profile` (spec §14). | MUST |
| FR-11.3 | Each tool MUST carry a docstring describing to an LLM what it does. | MUST |
| FR-11.4 | MCP tools MUST be read-only. | MUST |

### FR-12 Notifications

| Id | Requirement | Priority |
|---|---|---|
| FR-12.1 | The system MUST provide an internal inbox. No email is required (spec §23). | MUST |
| FR-12.2 | Notifications MUST be generated for invitations received, invitation responses and application status changes. | MUST |

### FR-13 Event sourcing

| Id | Requirement | Priority |
|---|---|---|
| FR-13.1 | Every meaningful state change MUST append an event carrying identifier, type, time, actor and data (blueprint §10). | MUST |
| FR-13.2 | The event log MUST be append-only; no UPDATE or DELETE. | MUST |
| FR-13.3 | Current state MUST be reconstructible by replaying the log. | MUST |
| FR-13.4 | The system MUST be able to display relevant action history. | MUST |

---

## 2. Non-functional requirements

### NFR-1 Architecture

| Id | Requirement |
|---|---|
| NFR-1.1 | The backend MUST be Flask (blueprint §9). |
| NFR-1.2 | The system MUST follow MVC with no mixing of layer responsibilities. |
| NFR-1.3 | Commands and queries MUST be separated conceptually and in implementation. |
| NFR-1.4 | A command MUST NOT return read data; a query MUST NOT mutate state. |
| NFR-1.5 | The domain layer MUST import no web framework and be testable with no app context. |
| NFR-1.6 | Only repositories may hold a database session. |

### NFR-2 Data

| Id | Requirement |
|---|---|
| NFR-2.1 | The transactional database MUST be cloud-hosted (blueprint §11). |
| NFR-2.2 | Entities, relationships, keys and constraints MUST be explicitly defined. |
| NFR-2.3 | The Vector DB MUST hold only curated knowledge, never transactional records. |
| NFR-2.4 | The schema MUST work on SQL Server 2014, which has no native JSON type. |

### NFR-3 Performance

| Id | Requirement |
|---|---|
| NFR-3.1 | No LLM call may occur in a request/response path. Measured CPU-only inference is 11–16 s per call, so ranking N candidates synchronously is not viable. |
| NFR-3.2 | Ranking MUST be computed by the deterministic scorer and returned immediately; explanations MUST be generated asynchronously. |
| NFR-3.3 | Completed analyses MUST be cached and not recomputed for unchanged inputs. |
| NFR-3.4 | A search results page MUST return within 2 seconds on the cloud database. |

### NFR-4 Security

| Id | Requirement |
|---|---|
| NFR-4.1 | Secrets MUST live in `.env`, which MUST be gitignored. No credential may appear in the repository or in documentation. |
| NFR-4.2 | Passwords MUST be hashed with a salted algorithm. |
| NFR-4.3 | Forms MUST be protected against CSRF. |
| NFR-4.4 | User-supplied content MUST be escaped on output. |
| NFR-4.5 | Outbound HTTPS MUST work behind the corporate TLS-inspecting proxy via the OS trust store. |

### NFR-5 Testing

| Id | Requirement |
|---|---|
| NFR-5.1 | Unit, integration, API, agent and E2E tests MUST all be present (blueprint §17). |
| NFR-5.2 | Every feature MUST have at least one negative test. |
| NFR-5.3 | Tests MUST NOT assert on generated LLM wording, only on structure and constraints. |
| NFR-5.4 | Scoring tests MUST run with no LLM and no network. |
| NFR-5.5 | Each test MUST document what it proves. |

### NFR-6 Usability

| Id | Requirement |
|---|---|
| NFR-6.1 | The interface MUST be responsive from 320px upward with no horizontal page scroll. |
| NFR-6.2 | Status MUST be conveyed by text as well as colour. |
| NFR-6.3 | Every image MUST carry descriptive alternative text. |
| NFR-6.4 | `prefers-reduced-motion` MUST be honoured. |
| NFR-6.5 | The interface MUST work in both light and dark colour schemes. |

### NFR-7 Code quality

| Id | Requirement |
|---|---|
| NFR-7.1 | `ruff check` MUST pass with no errors on project code. |
| NFR-7.2 | Public functions MUST carry type annotations and Google-convention docstrings. |
| NFR-7.3 | Nesting MUST NOT exceed three levels; guard clauses are required over nested happy paths. |
| NFR-7.4 | Names MUST be descriptive and unabbreviated. |

---

## 3. Out of scope

Explicitly excluded, from spec §3:

- Multiple organizations, marketplace or tenancy features
- Payments or fees
- Veterinary record management
- Social networking between adopters
- Email or SMS delivery
- Native mobile applications
