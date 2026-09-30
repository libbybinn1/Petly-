# PetMatch

An AI-assisted information system for an animal adoption organization.
Adopters discover animals that fit their profile and current intent; staff
discover the most suitable adopters for each animal, including proactive
outreach to people who opted in.

Built for *Software Engineering in the AI Era*. The specifications that drive
the code live in [`docs/`](docs/) — start with
[`docs/PRD.md`](docs/PRD.md) and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

**Presenting it?** [`docs/DEMO.md`](docs/DEMO.md) is the runbook: the nine demo
scenarios with the exact clicks, which account to use, what to point at, and
the honest list of known limitations.

---

## Important: the virtual environment lives outside this folder

```
C:\Users\libbyb\venvs\petmatch
```

**Do not create a `.venv` inside this project directory.** The project sits in
OneDrive, and OneDrive actively syncing a virtual environment corrupts it:
Python fails to read its own `site-packages`, producing different errors on
each run (`OSError: [Errno 22]`, then `AttributeError: Meta`, and so on). The
same test suite that fails inside OneDrive passes reliably from a venv kept
outside it.

Everywhere the documentation shows `<venv>\Scripts\python.exe`, use:

```
C:\Users\libbyb\venvs\petmatch\Scripts\python.exe
```

Rebuilding it from scratch:

```bash
python -m venv C:\Users\libbyb\venvs\petmatch
C:\Users\libbyb\venvs\petmatch\Scripts\python.exe -m pip install -r requirements.txt
```

---

## Running it

Copy `.env.example` to `.env` and fill in the values first. Every secret lives
there; none is in the repository.

```bash
set PY=C:\Users\libbyb\venvs\petmatch\Scripts\python.exe

# 1. verify every external dependency is reachable
%PY% scripts\check_environment.py

# 2. create the schema and load demo data   (see the warning below)
%PY% scripts\db.py fresh

# 3. embed the knowledge base into the vector database
%PY% scripts\ingest_knowledge.py

# 4. start everything: Ollama, the web application and the agent worker
%PY% scripts\start_all.py          # http://127.0.0.1:5000

# 5. stop it all again when you are done
%PY% scripts\stop_all.py
```

Steps 4 and 5 can still be done by hand, one process per terminal, which is
what `start_all.py` does for you:

```bash
%PY% run.py                        # http://127.0.0.1:5000
%PY% -m agent_service              # in a second terminal
```

> **Step 2 replaces all data and takes 9–12 minutes.** `db.py fresh` drops
> every table, recreates it, and loads 157 animals across eleven kinds, 40
> adopters and 3 staff — downloading a breed-accurate photograph for each
> animal from free public APIs and pacing itself to stay inside their rate
> limits. Where a download is not possible it falls back to a committed
> placeholder, so a fresh clone with no network still satisfies the
> "every animal has an image" rule.
>
> **Never run `db.py seed` over existing data.** `users.email` is unique and a
> second seed collides part-way through. `fresh` is the only safe reload.
>
> Step 3 embeds seventeen curated guides into 104 chunks.

`scripts/db.py` takes one command: `check`, `create`, `reset`, `seed`,
`fresh`, `tables` or `events`. There is no `migrate` — the schema is built
from the SQLAlchemy metadata, and there is no Alembic directory.

### Ollama does not start itself, by design

The Ollama shortcut has been removed from the Windows Startup folder. CPU-only
inference is the heaviest thing this project does — a loaded model holds
several gigabytes and saturates the processor while it generates — and there
is no reason for it to be doing that at login on a laptop that is also running
an IDE and a browser. `scripts/start_all.py` starts it on demand instead.

To put it back, copy the shortcut from
`%LOCALAPPDATA%\Ollama\Ollama.lnk.autostart-disabled` into
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\`.

### Always stop the servers you start

The Flask development server and the agent worker are long-lived foreground
processes, and closing a terminal or ending a debug session does not reliably
stop them. Left behind they keep holding their port, so the next launch either
cannot bind or — worse — the browser reaches a stale copy of the code and the
demo shows behaviour that is no longer in the repository.

`scripts/stop_all.py` clears them. It matches only processes launched from this
directory or this project's virtual environment, so unrelated `run.py` scripts
elsewhere on the machine are never touched; `tests/unit/test_process_control.py`
holds that property in place.

```bash
%PY% scripts\stop_all.py --dry-run          # list, change nothing
%PY% scripts\stop_all.py --include-ollama   # also free the model's memory
%PY% scripts\start_all.py --restart         # stop whatever is running, then start
```

### Recommended agent setting

```
AGENT_MAX_REASONING_STEPS=4
```

The default of 8 is a correctness bound. On this machine (CPU-only inference,
`qwen2.5:3b-instruct`) a tool-enabled turn costs 36–43 seconds warm and up to
145 seconds cold, so eight turns is several minutes for one analysis. Four is
ample: with the prerequisite evidence already in the prompt the model usually
answers on its first or second turn.

### Demo accounts

Password `Password123!`:

| Role | Email |
|---|---|
| Staff | `dana@petmatch.org` |
| Adopter | `maya@example.com` |

Maya arrives with an approved application for Smaug the bearded dragon and
three other applications closed by the §7.5 cascade, so the approval and
reopen rules have real data behind them from the first click.

## Tests and quality gates

```bash
%PY% -m pytest tests -q                    # 910 tests, all five suites
%PY% -m pytest tests/unit -q               # 333, offline, seconds
%PY% -m pytest -m "unit or api" -q         # the pre-commit loop
%PY% -m pytest tests/e2e -q                # 49, needs a Playwright browser

%PY% -m ruff check .
%PY% -m mypy --strict app agent_service mcp_server tests scripts
%PY% scripts\verify_requirements.py        # 23/23 mandatory requirements
```

`mypy --strict` covers `tests` and `scripts` as well as the application: a test
double whose signature has drifted from the protocol it stands in for passes at
runtime and proves nothing, and a seeding script that writes the wrong type
fails nine minutes into a reseed.

`scripts/verify_requirements.py` verifies by **inspecting behaviour** — it
builds the Flask app and reads its URL map, parses the code with `ast`, and
calls pure functions directly. It never touches the cloud database or a model.

See [`docs/TESTING.md`](docs/TESTING.md) for the full inventory and what each
suite proves.

## Layout

| Path | What is in it |
|---|---|
| `app/controllers/` | Six Flask blueprints: home, auth, animals, matches, personal, dashboard |
| `app/cqrs/` | `base.py` (the bus), `commands/` (18 handlers), `queries/` (19 handlers, and the view models) |
| `app/domain/` | Matching, application, invitation, profile and animal rules — no framework imports |
| `app/eventstore/` | `store.py` (append-only) and `projections.py` (replay and rebuild) |
| `app/infrastructure/` | SQLAlchemy models, engine, session factory |
| `app/templates/` | Jinja2 views |
| `agent_service/` | The independent agent process: worker, reason-act loop, RAG, tools |
| `mcp_server/` | Local MCP tool server, spoken over stdio |
| `knowledge/` | 17 curated guides embedded into the vector database |
| `docs/` | The eleven specification documents, plus the demo runbook |
| `scripts/` | Database, seeding, ingestion, environment and verification tooling |
| `tests/` | unit · integration · api · agent · e2e |
| `.claude/skills/` | Five skills guiding the coding agent |
| `CLAUDE.md` | The five binding rules |

### The screens

| Route | Who | What |
|---|---|---|
| `/` | anyone | Landing page with featured animals |
| `/animals/` | anyone | Structured search: species, size, age band, activity, city, compatibility |
| `/animals/<id>` | anyone | Details, with the apply form for an adopter |
| `/search/describe` | anyone | Describe what you want; the agent interprets it off the request path |
| `/my/profile` | adopter | The profile that drives matching, with the proactive opt-in |
| `/my/matches` | adopter | Find My Pet: ranked instantly, explained asynchronously |
| `/my/applications` | adopter | Own applications, with messages, statuses and history links |
| `/my/invitations` | adopter | Own invitations and the 72-hour countdown |
| `/my/notifications` | either | The internal inbox |
| `/animals/manage` | staff | The roster table |
| `/animals/new`, `/animals/<id>/edit` | staff | List and edit an animal |
| `/animals/<id>/adopters` | staff | Rank the people who applied, and decide |
| `/animals/<id>/discover` | staff | Find opted-in adopters who did not apply |
| `/analyses/<id>` | staff | One analysis in full, with the agent's reasoning trace |
| `/dashboard` | staff | Operational figures, needs-attention, the agent queue, recent activity |
| `/history/<type>/<id>` | staff, or the adopter it concerns | The event timeline for one record |

Full contract, including status codes and validation rules, in
[`docs/API.md`](docs/API.md).

## Architecture in one paragraph

Three processes. A Flask app in strict MVC with CQRS, writing to a cloud SQL
Server 2014 through an append-only event log. A separate agent process that
polls a job table, fetches records through MCP tools over stdio, retrieves
curated knowledge from ChromaDB, and gates web search behind an explicit
policy. And the MCP tool server itself, spawned as a subprocess.

**Scores are deterministic Python; the language model only writes the
explanation.** That is why a score can be justified line by line, why the test
suite runs offline, and why a model outage degrades the product to scores
without prose rather than to no service at all.
