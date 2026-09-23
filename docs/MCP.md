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

That boundary is what keeps the tool surface honest. The agent process does
import the ORM models, because the job queue is a database table it has to
poll (`agent_service/worker.py`) and the MCP server itself reads records
through them (`mcp_server/server.py`). What the agent does **not** have is any
path to the Flask application, a controller or a command — so it can read the
records it is advising about and change nothing. docs/AGENT.md §2 states that
boundary precisely.

## 3. Transport

```
Agent process                         MCP server process
─────────────                         ──────────────────
mcp.client.stdio  ──── stdin  ────▶   mcp.server (MCPServer.run("stdio"))
                  ◀─── stdout ─────
```

The server is built on `mcp.server.mcpserver.MCPServer` and started with
`mcp_server.run(transport="stdio")`. The agent spawns it as a subprocess using
**the interpreter it is itself running under** (`sys.executable`), so no path
is hardcoded:

```python
StdioServerParameters(command=sys.executable, args=["-m", "mcp_server"], cwd=PROJECT_ROOT)
```

Communication is newline-delimited JSON-RPC over the pipes. Nothing is
exposed on a network port, which is what "local tools over stdio" means.

**stdout is the protocol channel.** The server must never `print()` to it; all
diagnostics go to stderr. A stray print corrupts the JSON-RPC stream, and the
symptom — a client that hangs rather than errors — is hard to diagnose. The
`clean-code` skill already bans `print()` in application code, and ruff's
`T20` rule enforces it.

Sessions are short-lived by design: a tool call is one request and one
response, and holding a subprocess open across a long analysis would add
failure modes for no benefit. The one exception is the tool *listing*, which
is cached on the client after the first success — the advertised tools cannot
change while the server binary does not, and every listing costs a process
start.

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
says what the tool returns and when to use it — including when *not* to
("Do not use this to search for adopters") — because that text is what the
model sees when deciding whether to call it.

That is now literally true rather than aspirational. `McpToolClient.list_tool_definitions`
reads each tool's name, description and input schema from the running server,
and `agent_service.loop.build_tool_manifest` turns them into the manifest sent
with every model turn. Editing a docstring in `mcp_server/server.py` therefore
changes what the model is told, with no second copy to keep in step.

**Serialisable output only.** Plain JSON types. No ORM objects, no datetimes
that have not been formatted, no enums that have not been converted.

## 6. Web search and MCP

Spec §15 asks whether web search should also be an MCP tool. The answer taken
here: the **local stdio tools are the graded requirement**, so they are done
properly; web search is implemented in the simplest form that demonstrates
the behaviour — a direct Tavily call behind a policy gate, described in
`AGENT.md` §10. The project deliberately does not take on a complicated
external MCP ecosystem, which spec §15 also warns against.

The model does not see that difference. `rag_search` and `web_search` are
declared in `agent_service/loop.py` and appear in the same manifest as the two
MCP tools, with descriptions written the same way. What differs is the
transport and, for the web, the policy gate that can refuse the call and tell
the model which rule refused it.

## 7. Testing

`tests/agent/test_mcp_stdio.py` spawns the **real server as a subprocess**
and round-trips both tools over stdio. It does not mock the transport —
mocking it would leave the one thing the requirement is about untested.

| Test | Proves |
|---|---|
| `test_server_advertises_both_tools` | Both tools are advertised, each with a description long enough to guide a model. |
| `test_get_adopter_profile_round_trips` | A real adopter record travels back over stdio intact, with every field the scorer consumes. |
| `test_get_animal_profile_round_trips` | A real animal record travels back over stdio intact. |
| `test_unknown_identifier_returns_structured_not_found` | A missing record yields `found: false`, not a protocol error. |
| `test_tools_expose_no_mutating_operation` | No tool name describes a write. |

These tests are marked `slow`: each starts a process that opens the cloud
database, so they need the network. The spawn is retried once, because under
load it has been seen to fail before the session is ready — a second failure
is still raised, since a broken transport is what these tests exist to catch.

`tests/agent/test_reasoning_loop.py` covers the client side without a
subprocess: that the manifest carries both tool names with non-empty
descriptions, that a model-chosen record lookup is dispatched, and that a tool
server which cannot be listed leaves the agent working with its local tools.

## 8. Running it by hand

```bash
# start the server on its own (it will wait on stdin)
<venv>/Scripts/python.exe -m mcp_server

# or let the agent spawn it, which is the normal path
<venv>/Scripts/python.exe -m agent_service
```

The interpreter lives outside the project directory; README.md explains why a
virtual environment inside OneDrive corrupts itself.
