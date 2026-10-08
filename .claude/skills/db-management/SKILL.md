---
name: db-management
description: Connect to, create, reset, seed and inspect the PetMatch database. Use whenever working with the Somee.com SQL Server database, rebuilding the schema, loading seed data, or diagnosing connection problems.
---

# Database Management — PetMatch

One entry point for every database operation: `scripts/db.py`.

```bash
C:\Users\libbyb\venvs\petmatch\Scripts\python.exe scripts\db.py <command>
```

The interpreter is **outside** the project: OneDrive corrupts a virtual
environment it syncs. Never create a `.venv` inside this directory - see
README.md.

## Commands

| Command | What it does |
|---|---|
| `check` | Verify connectivity, print server version, table count and write permission |
| `create` | Create every table that does not yet exist |
| `reset` | Drop every project table, then recreate it from the SQLAlchemy metadata |
| `seed` | Load demo data: animals, adopters, applications, invitations, events |
| `fresh` | `reset` + `seed` in one step - the only safe way to reload |
| `tables` | List project tables with row counts |
| `events` | Show the most recent domain events (event-sourcing inspection) |

**There is no `migrate`, `revision` or `downgrade`, and there is no Alembic
directory.** The schema is created from `Base.metadata`. One developer, one
database and a rebuild that takes a minute make a migration history a cost
with no payer; `requirements.txt` still pins Alembic, and that pin is
vestigial.

**Two warnings that matter more than the table.**

- `fresh` **replaces all data** and takes **9-12 minutes**, because seeding
  downloads a breed-accurate photograph for each of 157 animals from free
  public APIs and paces itself inside their rate limits. Never run it during
  a demonstration.
- Never run `seed` over existing data. `users.email` is unique, so a second
  seed collides part-way through and leaves a half-loaded database. `fresh`
  is the only safe reload.

## Target database

**Somee.com — Microsoft SQL Server 2014 Express (SP3)**

```
host:     <DB_SERVER>    e.g. <instance>.mssql.somee.com
database: <DB_NAME>
login:    <DB_USER>
driver:   pymssql  (no system ODBC driver needed)
url:      mssql+pymssql://<user>:<pass>@<server>/<database>
```

Every value above comes from `.env`, which is gitignored. Never hardcode them,
never commit them, and never write the real host, database or login into
documentation - this file is published. Run `scripts/check_environment.py` to
confirm the configured connection works.

## SQL Server 2014 constraints — read before writing models

This is an old engine. These limits are real and have already shaped the schema:

- **No native `JSON` type.** JSON functions arrived in SQL Server 2016. Event
  payloads and match-analysis blobs are stored as `NVARCHAR(MAX)` and
  serialized/deserialized in Python. Never write `ISJSON()`, `JSON_VALUE()` or
  `OPENJSON()`.
- **No `STRING_AGG`** (2017+). Aggregate string concatenation in Python instead.
- **No `IDENTITY` reseed inside a transaction** on some Somee plans — prefer
  application-generated UUIDs for aggregate identifiers, which also suits
  event sourcing.
- `OFFSET ... FETCH NEXT` **is** available (2012+), so keyset pagination is fine.
- **Free-tier size cap.** Keep images on local disk and store only the URL.
  Never put binary blobs in this database.
- Shared host: connections can be slow or briefly refused. Retry logic belongs
  in the engine configuration, not scattered through call sites.

## Local development fallback

Set `LOCAL_DATABASE_URL` in `.env` to work offline:

```
LOCAL_DATABASE_URL=sqlite:///petmatch_dev.db
```

When present it overrides the cloud database.

**Be accurate about what runs where.** Unit tests touch no database at all.
Integration and API tests build an **in-memory SQLite** database per test. The
E2E suite starts the server against a **seeded SQLite file**, because Somee's
free tier throttles under a browser page's request fan-out. What genuinely
exercises the SQL Server 2014 dialect is `scripts/check_environment.py`,
`scripts/db.py` and the running application.

That trade-off has one sharp edge worth remembering when writing validation:
**SQLite accepts a string longer than its column; SQL Server truncates or
errors.** Length rules therefore have to be enforced in Python, and
`tests/unit/test_seed_data.py` reads the column widths off the ORM models to
check them.

## Troubleshooting

**TDS 20002 / "TDS server connection failed"** — the TCP port is often open and
TLS succeeds; FreeTDS then fails converting the login packet (error 2402)
because its Windows iconv cannot use the default UTF-8 client charset. The
application URL and `check_environment.py` pass `charset=CP1252`. A raw
`pymssql.connect(...)` without that argument will still fail on this machine.

**Connection refused / timeout** — Somee's free tier throttles. Run `db.py check`.
If TCP reaches port 1433 but login fails *after* the charset is set, the
account may be sleeping; open the Somee control panel once to wake it.

**"Login failed for user"** — confirm `.env` matches the Somee panel exactly. The
password is case-sensitive.

**A reset or seed hangs** - an open transaction from a crashed run holds a
schema lock. Reconnect and retry; the shared host clears it within a minute.
