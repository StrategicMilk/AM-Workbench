# MCP Security Model

This document describes how AM Workbench controls access to MCP (Model Context Protocol)
tools — both the built-in tools exposed by AM Workbench's own MCP server and the external
tools loaded from third-party MCP servers.

---

## Which MCP Tools Are Allowed

### Built-in AM Workbench tools

Five tools are always available through AM Workbench's own MCP server.  They are
registered unconditionally in `MCPToolRegistry.register_defaults()`:

| Tool name | Capability |
|---|---|
| `vetinari_plan` | Generate an execution plan from a goal description |
| `vetinari_search` | Semantic codebase search via CocoIndexAdapter |
| `vetinari_execute` | Execute a task through the AM Workbench pipeline |
| `vetinari_memory` | Query or store entries in the dual memory system |
| `vetinari_benchmark` | Run a named benchmark suite and return a summary |

These tools are available to any MCP client that can authenticate as an admin
(see [Admin guard](#admin-guard-on-mcp-endpoints) below).

### External server tools

External tools can be loaded by the Worker MCP bridge from `config/mcp_servers.yaml`.
Only servers listed in that file with `enabled: true` are connected by that
client-side bridge. Those tools are not automatically exposed through the
served `/mcp/tools` or JSON-RPC `tools/list` registry unless the server-side
`MCPToolRegistry.register_external_server()` path is explicitly wired for that
running client. Document this as a split between Worker-consumed external MCP
tools and tools exposed to remote MCP clients until Session 34F2 proves a
single shared runtime registry.

To add, remove, or disable an external server, edit `config/mcp_servers.yaml`
and restart the AM Workbench server.  No code changes are required.

**Example — disabling the filesystem server:**

```yaml
mcp_servers:
  - name: filesystem
    command: npx
    # /tmp is a valid path on Linux/macOS.  On Windows use a full backslash
    # path (e.g. "C:\\Users\\<user>\\AppData\\Local\\Temp") or a WSL path.
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    env: {}
    enabled: false   # disabled — no filesystem tools will be registered
```

---

## Input Validation

### JSON Schema publication

Every MCP tool declares an `inputSchema` in the JSON Schema format required by
the MCP specification.  The schema is exposed via `GET /mcp/tools` and is also
visible to LLM callers via the `tools/list` JSON-RPC method.

The schema is enforced for the simple JSON Schema subset represented by
`MCPToolParameter`: required fields, scalar JSON types, object/array types, and
`additionalProperties: false`. Invalid arguments return bounded MCP tool errors
before handler dispatch.

### Request body validation on POST /mcp/message

The `POST /mcp/message` handler reads the raw request body and validates it
before dispatching:

1. Empty body → `400 {"error": "Request body must be a JSON object"}`
2. Malformed JSON → `400 {"error": "Invalid JSON"}`
3. Valid JSON but not an object (string, array, number) → `400 {"error": "Request body must be a JSON object"}`
4. Valid JSON object → dispatched to `MCPServer.handle_message()`

This keeps MCP request validation inside the `POST /mcp/message` handler and
returns structured 400 errors when callers send non-object JSON bodies.

---

## Output Sanitization

Tool results returned from external MCP servers are treated as **untrusted
text**.  The `WorkerMCPBridge.invoke_mcp_tool()` method returns the raw text
content block from the external server without further interpretation.

Callers (including the Worker agent) are responsible for:

- Not executing tool output as code without a separate approval step
- Not treating tool output as authoritative unless the source server is trusted
- Logging tool outputs at DEBUG level, not INFO, to avoid leaking sensitive
  data into production log aggregators

The MCP client never parses or evaluates the `text` field of a content block
beyond JSON decoding.  Structured data returned by external tools is the
caller's responsibility to validate.

---

## Local-User Guard on MCP Endpoints

HTTP endpoints in `crates/amw-kernel/src/api/routes/workbench_domains.rs` are
owned by the Rust kernel. Framework-neutral MCP support remains in
`vetinari/mcp/http_transport.py`:

| Endpoint | Method | Guard |
|---|---|---|
| `/mcp/message` | POST | `local_user_guard` |
| `/mcp/tools` | GET | `local_user_guard` |
| `/mcp/resources` | GET | `local_user_guard` |
| `/mcp/resources/read` | GET | `local_user_guard` |
| `/mcp/resources/stream` | GET | `local_user_guard` |

The `local_user_guard` permits loopback callers for local-first use. Remote
callers must authenticate with the same `VETINARI_ADMIN_TOKEN` accepted by
`admin_guard`, using `X-Admin-Token` or `Authorization: Bearer <token>`. Treat
this as a local trusted-operator boundary, not a multi-user or internet-facing
authorization model.

The stdio transport (`vetinari mcp --transport stdio`) runs as a subprocess
with the same privileges as the parent process.  It should only be connected
to trusted callers (e.g. a local editor plugin).

---

## Threat Model Summary

| Threat | Mitigation |
|---|---|
| Unauthenticated access to MCP endpoints | `admin_guard` on all `/mcp/` routes |
| Non-object or malformed JSON-RPC bodies | Manual body parsing with structured 400 responses |
| Unknown tool names | `invoke()` returns `{"error": "Unknown tool"}` — no reflection |
| External server returning malicious text | Output treated as untrusted text; never eval'd |
| Unauthorized external server config | Only servers in `config/mcp_servers.yaml` are connected |
| Subprocess escape via command injection | Commands are list-based (`subprocess.Popen(list)`) — no shell expansion |
