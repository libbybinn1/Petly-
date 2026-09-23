# PetMatch

An AI-assisted information system for an animal adoption organization.
Adopters discover animals that fit their profile and current intent; staff
discover the most suitable adopters for each animal, including proactive
outreach to people who opted in.

Built for *Software Engineering in the AI Era*. The specifications that drive
the code live in [`docs/`](docs/) — start with
[`docs/PRD.md`](docs/PRD.md) and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

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

Everywhere the documentation says `.venv/Scripts/python.exe`, use:

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

Set up `.env` first by copying `.env.example` and filling in the values.

```bash
# 1. verify every external dependency is reachable
<venv>\Scripts\python.exe scripts\check_environment.py

# 2. create the schema and load demo data
<venv>\Scripts\python.exe scripts\db.py fresh

# 3. embed the knowledge base into the vector database
<venv>\Scripts\python.exe scripts\ingest_knowledge.py

# 4. start the web application
<venv>\Scripts\python.exe run.py            # http://127.0.0.1:5000

# 5. in a second terminal, start the independent agent process
<venv>\Scripts\python.exe -m agent_service
```

Demo accounts, password `Password123!`:

| Role | Email |
|---|---|
| Staff | `dana@petmatch.org` |
| Adopter | `maya@example.com` |

## Tests

```bash
<venv>\Scripts\python.exe -m pytest tests/ -q          # everything
<venv>\Scripts\python.exe -m pytest tests/unit -q      # fast, no I/O
<venv>\Scripts\python.exe -m ruff check .
```

## Layout

| Path | What is in it |
|---|---|
| `app/` | Flask application: controllers, CQRS, domain, event store, repositories |
| `agent_service/` | The independent agent process: loop, RAG, tools |
| `mcp_server/` | Local MCP tool server, spoken over stdio |
| `knowledge/` | Curated guides embedded into the vector database |
| `docs/` | The eleven specification documents |
| `scripts/` | Database, seeding, ingestion and environment tooling |
| `tests/` | unit · integration · api · agent · e2e |
| `.claude/skills/` | Skills guiding the coding agent |
| `CLAUDE.md` | The five binding rules |

## Architecture in one paragraph

Three processes. A Flask app in strict MVC with CQRS, writing to a cloud SQL
Server through an append-only event log. A separate agent process that polls a
job table, fetches records through MCP tools over stdio, retrieves curated
knowledge from ChromaDB, and gates web search behind an explicit policy. And
the MCP tool server itself, spawned as a subprocess.

**Scores are deterministic Python; the language model only writes the
explanation.** That is why a score can be justified line by line, why the test
suite runs offline, and why a model outage degrades the product to scores
without prose rather than to no service at all.
