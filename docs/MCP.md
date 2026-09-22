# MCP — PetMatch

**Local tool server, transport and tool contracts.**
Sources: course blueprint §8; PetMatch spec §14, §15.

---

## 1. Requirement

Blueprint §8 requires **at least two local tools**, implemented with MCP over
**stdio** — explicitly "not Studio" — each carrying a description that tells
an LLM what it can do.

PetMatch provides exactly the two the product specification names (§14):

| Tool | Purpose |
|---|---|
| `get_adopter_profile` | Retrieve the structured adopter profile needed for matching |
| `get_animal_profile` | Retrieve the structured animal profile needed for matching |

Spec §14 warns against padding the count: a third tool
(`get_adoption_applications`) is added only if a real need appears, not to
reach a number.

## 2. Why these two

The architectural point is stated in spec §14: *the agent does not directly
own the application's data; it requests the information through tools.*

That boundary is what makes the agent genuinely separate. Without it the
agent would import the ORM, share the Flask app's session and stop being an
independent component — it would be a background thread with extra steps.

## 3. Transport

```
Agent process                         MCP server process
─────────────                         ──────────────────
mcp.client.stdio  ──── stdin  ────▶   mcp.server.stdio
                  ◀─── stdout ─────
```

The agent spawns the server as a subprocess:

```bash
.venv/Scripts/python.exe -m mcp_server
```

Communication is newline-delimited JSON-RPC over the pipes. Nothing is
exposed on a network port, which is what "local tools over stdio" means.

**stdout is the protocol channel.** The server must never `print()`; all
diagnostics go to stderr. A stray print corrupts the JSON-RPC stream, and the
symptom — a client that hangs rather than errors — is hard to diagnose. The
`clean-code` skill already bans `print()` in application code, and ruff's
`T20` rule enforces it.

## 4. Tool contracts

### get_adopter_profile

```
Parameters:  adopter_profile_id : str   (UUID)
Returns:     the adopter's matching-relevant fields, or a not-found result
```

Returns home type, yard, children and youngest child age, other animals,
experience level, activity level, daily hours available, city, preferred
species, the proactive-suggestions opt-in and profile completeness.

### get_animal_profile

```
Parameters:  animal_id : str   (UUID)
Returns:     the animal's matching-relevant fields, or a not-found result
```

Returns name, species, breed, age, size, temperament, activity level, child
and other-animal compatibility, special needs and their description, required
space, city and adoption status.

## 5. Design rules

**Read-only.** Neither tool writes. State changes belong to commands in the
Flask application, where the business rules and the event store live. A tool
that could mutate would let the agent change the system it is supposed to be
advising about.

**Structured not-found, never an exception.** A missing record returns
`{"found": false, "reason": "..."}`. An exception across the MCP boundary
becomes an opaque protocol error the model cannot reason about; a structured
answer is something it can act on and report as missing information.

**Descriptions are written for a model, not a developer.** Each docstring
says what the tool returns and when to use it, because that text is the only
thing the model sees when deciding whether to call it.

**Serialisable output only.** Plain JSON types. No ORM objects, no datetimes
that have not been formatted, no enums that have not been converted.

## 6. Web search and MCP

Spec §15 asks whether web search should also be an MCP tool. The answer taken
here: the **local stdio tools are the graded requirement**, so they are done
properly; web search is implemented in the simplest form that demonstrates
the behaviour — a direct Tavily call behind a policy gate, described in
`AGENT.md` §8. The project deliberately does not take on a complicated
external MCP ecosystem, which spec §15 also warns against.

## 7. Testing

`tests/agent/test_mcp_stdio.py` spawns the **real server as a subprocess**
and round-trips both tools over stdio. It does not mock the transport —
mocking it would leave the one thing the requirement is about untested.

| Test | Proves |
|---|---|
| `test_server_lists_both_tools` | Both tools are advertised with descriptions. |
| `test_get_adopter_profile_returns_record` | A known adopter round-trips over stdio. |
| `test_get_animal_profile_returns_record` | A known animal round-trips over stdio. |
| `test_unknown_id_returns_structured_not_found` | A missing record yields `found: false`, not an exception. |
| `test_tools_are_read_only` | No tool exposes a mutating operation. |

## 8. Running it by hand

```bash
# start the server on its own (it will wait on stdin)
.venv/Scripts/python.exe -m mcp_server

# or let the agent spawn it, which is the normal path
.venv/Scripts/python.exe -m agent_service
```
