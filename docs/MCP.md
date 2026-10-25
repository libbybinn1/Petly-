# MCP — PetMatch

**Local tool server, transport and tool contracts, and the external Tavily MCP server.**
Sources: course blueprint §8; course requirements 9 and 10; PetMatch spec §13, §14, §15.

---

## 1. Requirement

Course requirement 10 asks for **at least two tools developed with MCP**
("MCP – Studio"), and blueprint §8 adds that they are local tools, each
carrying a description that tells an LLM what it can do.

PetMatch meets both readings. The two tools are developed as a real MCP
server — `mcp.server.mcpserver.MCPServer` (the MCP 2.x name for FastMCP) in
`mcp_server/server.py`, speaking the **stdio** transport — and, because they
are a standard MCP server, they can be opened and exercised interactively in
the **MCP Inspector** ("MCP Studio"); §8 gives the command. The agent itself
always reaches them over stdio.

Course requirement 9 — external services are consumed via the MCP protocol —
is covered separately in §9: web search goes to **Tavily's hosted MCP
server**, not to a REST SDK.

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

Spec §15 asks whether web search should also be an MCP tool. Course
requirement 9 settles it: external services are consumed via MCP. Web search
is therefore an **MCP client call to Tavily's remote MCP server** (§9),
behind the same spec §13 policy gate described in `AGENT.md` §10. The project
consumes one external server and nothing more, keeping to spec §15's warning
against a complicated external MCP ecosystem.

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

These tests are marked `slow`, because each starts a real subprocess. They run
against a **temporary SQLite database the test seeds itself**, not against
Somee — which makes them deterministic and, more importantly, means they
**never skip**. They used to open the cloud database and skip when it held no
seed data, and a transport test that quietly skips is exactly the one you
cannot afford to lose: the transport is the whole graded requirement.

The spawn is retried once, because under load it has been seen to fail before
the session is ready — a second failure is still raised, since a broken
transport is what these tests exist to catch.

`tests/agent/test_tavily_mcp.py` covers the external Tavily MCP client
offline: result parsing from a recorded `tavily_search` result, the
400-character snippet limit, discovery of the tool and its arguments from the
advertised schema, an in-payload error (how Tavily reports a rejected key),
degradation to `[]` on any failure, and that the API key appears in no
message or log line. One test drives the real Streamable HTTP client against
an in-process mock transport and proves the key is sent as a bearer header
and never in the URL. No test touches the network.

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

### In the MCP Inspector ("MCP Studio")

The MCP Inspector is the protocol's interactive test bench: it launches a
server, lists its tools with their descriptions and schemas, and lets you
call each one with hand-entered arguments. From the project root:

```bash
npx @modelcontextprotocol/inspector C:\Users\libbyb\venvs\petmatch\Scripts\python.exe -m mcp_server
```

It needs **Node.js** (for `npx`), which is not installed on the development
machine; the agent and the test suite do not need it. The server reads the
database configured in `.env`, so the tools return live records.

The interpreter lives outside the project directory; README.md explains why a
virtual environment inside OneDrive corrupts itself.

## 9. External MCP server: Tavily web search

Course requirement 9: external services are consumed via MCP. Web search is
the one external service PetMatch uses, and it is consumed as an MCP client
of Tavily's hosted MCP server.

| Aspect | Value |
|---|---|
| Server | Tavily remote MCP server, hosted by Tavily |
| Transport | **Streamable HTTP** (`mcp.client.streamable_http.streamable_http_client`, `mcp==2.2.0`) |
| URL | `https://mcp.tavily.com/mcp/` |
| Authentication | `Authorization: Bearer <TAVILY_API_KEY>` header |
| Tool | `tavily_search`, found by name in the live `tools/list`, not assumed |
| Arguments sent | `query`, and `max_results` when the advertised schema declares it |
| Code | `agent_service/tools/tavily_mcp.py` (wire), `agent_service/tools/web_search.py` (`TavilyMcpSearchProvider`, policy) |

**Why remote rather than stdio.** Tavily's stdio server is an npm package and
needs Node.js, which the demo machine does not have. The hosted server
exposes the same tools over Streamable HTTP, which the Python `mcp` client
speaks natively. No extra dependency is needed, and the `tavily-python` REST
SDK was removed from `requirements.txt`.

**The key never enters a URL.** Tavily also accepts the key as a
`tavilyApiKey` query parameter, but a URL is what HTTP libraries and the MCP
SDK itself write to debug logs and exception messages. The key is sent only
as a header, and every failure message is additionally scrubbed of it (raw
and URL-encoded) before it is logged or stored.

**Each search is one session:** `initialize`, `tools/list`, `tools/call`,
close, the same short-lived pattern as the local tools (§3). The server
advertises `tavily_search`, `tavily_extract`, `tavily_crawl`, `tavily_map` and
`tavily_research`; only `tavily_search` is used.

**Result mapping.** The tool returns one JSON object, both as
`structured_content` and as a text block. The structured form is read first,
the text block is the fallback. Each entry of its `results` list becomes a
`SearchResult`:

| Tavily field | `SearchResult` field |
|---|---|
| `title` | `title` |
| `url` | `url` (a hit with no URL is dropped: the agent may only cite real sources) |
| `content` | `snippet`, cut to 400 characters |

**Failure behaviour.** Any failure (network, TLS, HTTP status, an `is_error`
result, an `error` object inside the payload, which is how Tavily reports a
rejected key, or a missing `tavily_search` tool or `query` argument) makes
`TavilyMcpSearchProvider.search` return `[]`. The agent proceeds with the
evidence it has and records the gap as missing information. The scrubbed
reason is logged as a warning and kept in `last_failure`. With no
`TAVILY_API_KEY` configured, `build_search_provider` returns the offline
`StubSearchProvider` instead.

**Still behind the RAG-first gate.** Moving the transport to MCP changes
nothing about *when* the web is searched. `decide_whether_to_search` runs
first and refuses whenever the curated knowledge base answered, whenever the
question is about the application's own records (those come from the local
MCP tools in §4), and after one search per job (spec §13, CLAUDE.md R4).

**Corporate network.** The call goes through the SSL-inspecting firewall
like any HTTPS request; `truststore.inject_into_ssl()` in `app/config.py` and
`agent_service/__main__.py` makes it verify. `scripts/check_environment.py`
runs one live search through this same client to confirm it.
