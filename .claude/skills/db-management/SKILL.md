---
name: db-management
description: Connect to, migrate, reset, seed and inspect the PetMatch database. Use whenever working with the Somee.com SQL Server database, running Alembic migrations, loading seed data, or diagnosing connection problems.
---

# Database Management — PetMatch

One entry point for every database operation: `scripts/db.py`.

```bash
.venv/Scripts/python.exe scripts/db.py <command>
```

## Commands

| Command | What it does |
|---|---|
| `check` | Verify connectivity, print server version, table count and write permission |
| `migrate` | Apply all pending Alembic migrations |
| `revision -m "msg"` | Autogenerate a new migration from model changes |
| `downgrade` | Roll back one migration |
| `reset` | Drop every project table, re-run migrations from scratch |
| `seed` | Load demo data: animals, adopters, applications, invitations |
| `fresh` | `reset` + `seed` in one step — the usual "give me a clean demo" command |
| `tables` | List tables with row counts |
| `events` | Tail the `domain_events` log (event sourcing inspection) |

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

When present it overrides the cloud database. Unit tests always use SQLite
in-memory; integration tests run against the real Somee database so the SQL
Server dialect is genuinely exercised.

## Troubleshooting

**Connection refused / timeout** — Somee's free tier throttles. Run `db.py check`.
If TCP reaches port 1433 but login fails, the account may be sleeping; open the
Somee control panel once to wake it.

**"Login failed for user"** — confirm `.env` matches the Somee panel exactly. The
password is case-sensitive.

**Migration hangs** — an open transaction from a crashed run holds a schema lock.
Reconnect and retry; the shared host clears it within a minute.
