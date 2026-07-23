# MCP Server Guide

AM Workbench exposes a Model Context Protocol server so trusted local clients
can call selected Workbench capabilities through JSON-RPC. This page covers the
user-facing setup and operating contract. For threat modeling and request
validation details, see [MCP Security Model](mcp-security.md).

## Supported Transports

| Transport | Command or endpoint | Status | Use when |
|---|---|---|---|
| stdio | `python -m vetinari mcp --transport stdio` | Supported | A trusted local editor or assistant launches AM Workbench as a subprocess. |
| Streamable HTTP JSON-RPC | `POST /mcp` on the native Rust kernel server | Supported | A Streamable HTTP client talks to the local MCP endpoint with one JSON-RPC request per POST. |
| HTTP JSON-RPC | `POST /mcp/message` on the native Rust kernel server | Supported | A trusted local client talks to a running `vetinari serve` process. |
| HTTP tool list | `GET /mcp/tools` on the native Rust kernel server | Supported | A client or operator needs to inspect available tool schemas. |
| HTTP resource list/read | `GET /mcp/resources`, `GET /mcp/resources/read` | Supported | A trusted local client needs one-shot MCP resource discovery or reads. |
| HTTP+SSE resource stream | `GET /mcp/resources/stream` with `Accept: text/event-stream` | Supported | A trusted local client subscribes to resource events with SSE event IDs. |

`vetinari mcp --transport http` is only an orientation command. It does not
start a separate MCP HTTP server. Start the native Rust kernel server with
`python -m vetinari serve --host 127.0.0.1 --port 5000`, then send MCP
JSON-RPC requests to `POST /mcp` or `POST /mcp/message`.

## Authentication Boundary

The HTTP MCP endpoints use Vetinari's local-user guard: loopback clients are
allowed for local-first use, and remote clients must present the configured
admin token. For remote or proxied operator use, set an admin token before
starting the server:

```powershell
$env:VETINARI_ADMIN_TOKEN = "<choose-a-local-admin-token>"
.venv312/Scripts/python.exe -m vetinari serve --host 127.0.0.1 --port 5000
```

Send either `Authorization: Bearer <token>` or `X-Admin-Token: <token>` with
HTTP requests. Keep the server bound to `127.0.0.1` unless you have configured
trusted proxy and remote-read/mutation controls. The stdio transport inherits
the privileges of the parent process and should only be connected to trusted
local clients.

## OAuth And Marketplace Posture

AM Workbench implements PKCE authorization-request construction, OAuth/OIDC
authorization-code token exchange, redacted token serialization, and
bearer-authenticated Streamable HTTP install probes for marketplace rows that
declare OAuth metadata. Dynamic client registration and hosted MCP marketplace
publication are not claimed.

Extension and MCP marketplace rows in
`config/workbench/extension_marketplace.yaml` are metadata and risk decisions.
Imported rows are disabled by default, manual-selection gated, and evaluated by
Workbench-owned risk verdicts before any registration decision. Token values are
not exposed through public API serialization; transport clients receive bearer
headers only through explicit install/probe calls.

Admin-gated OAuth routes:

| Route | Purpose |
|---|---|
| `POST /api/workbench/extensions/{extension_id}/oauth/authorization-request` | Build a PKCE authorization URL from the catalog row's OAuth metadata. |
| `POST /api/workbench/extensions/{extension_id}/oauth/token` | Exchange an authorization code for redacted token metadata. |

The marketplace CLI also accepts `--oauth-code`, `--oauth-redirect-uri`,
`--oauth-client-id`, and `--oauth-code-verifier` for install-time token
exchange before MCP transport probing.

## Built-In Tools

The server registers five built-in AM Workbench tools:

| Tool | Purpose | Required input |
|---|---|---|
| `vetinari_plan` | Generate an execution plan from a goal. | `goal` |
| `vetinari_search` | Search the codebase with semantic search when available. | `query` |
| `vetinari_execute` | Execute a task through the Workbench pipeline. | `task` |
| `vetinari_memory` | Store or recall Workbench memory entries. | `action`, `content` |
| `vetinari_benchmark` | Run a named benchmark suite. | `suite` |

Tool schemas are available through `GET /mcp/tools` and through the MCP
`tools/list` JSON-RPC method after initialization. Arguments are validated
before tool dispatch. Missing required fields, unsupported fields, or wrong
JSON scalar types return bounded tool errors rather than executing the handler.

## Stdio Client Setup

Use stdio when the client can launch a command:

```json
{
  "command": "<repo-root>/.venv312/Scripts/python.exe",
  "args": ["-m", "vetinari", "mcp", "--transport", "stdio"],
  "cwd": "<repo-root>"
}
```

The stdio server writes its ready message to stderr and reads JSON-RPC frames
from stdin. If the client stalls waiting for a response, stop the subprocess
and check the client log for malformed JSON-RPC or an initialization failure.

## HTTP Client Smoke Test

Start the server, then request the tool list:

```powershell
$headers = @{ Authorization = "Bearer $env:VETINARI_ADMIN_TOKEN" }
Invoke-RestMethod -Method Get -Uri http://127.0.0.1:5000/mcp/tools -Headers $headers
```

A Streamable HTTP JSON-RPC initialization request uses `POST /mcp`:

```powershell
$body = @{
  jsonrpc = "2.0"
  id = 1
  method = "initialize"
  params = @{ protocolVersion = "2025-11-25"; capabilities = @{} }
} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:5000/mcp -Headers $headers -Body $body -ContentType "application/json"
```

A non-object body, malformed JSON, oversized key, or excessive nesting is
rejected before dispatch.

## HTTP+SSE Resource Streaming

Resource streaming is session-bound. A trusted HTTP client first sends
`initialize` through `POST /mcp` or `POST /mcp/message` and declares a safe resource session
ID under `capabilities.vetinari.resourceSessionId`. Resource permissions are
declared in `capabilities.permissions` or `capabilities.vetinari.permissions`.

```powershell
$sessionId = "local-client-1"
$body = @{
  jsonrpc = "2.0"
  id = "init-1"
  method = "initialize"
  params = @{
    protocolVersion = "2025-11-25"
    capabilities = @{
      permissions = @{ "resource:workspace" = $true }
      vetinari = @{ resourceSessionId = $sessionId }
    }
  }
} | ConvertTo-Json -Depth 8
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:5000/mcp/message -Headers $headers -Body $body -ContentType "application/json"
```

The initialize response includes `capabilities.resources.subscribe = true`
and echoes `result.vetinari.resourceSessionId`. Clients can then read or
subscribe to resources with that session ID:

```powershell
Invoke-WebRequest -Uri "http://127.0.0.1:5000/mcp/resources/read?session_id=$sessionId&uri=resource://workspace/context&correlation_id=read-1" -Headers @{ Accept = "text/event-stream" }
Invoke-WebRequest -Uri "http://127.0.0.1:5000/mcp/resources/stream?session_id=$sessionId&uri=resource://workspace/context&correlation_id=stream-1" -Headers @{ Accept = "text/event-stream" }
```

The stream emits `resources/subscribed`, retained resource events such as
`resources/read`, and keepalive comments. Reconnecting clients may send the
standard `Last-Event-ID` header to resume after the last observed retained
event. Malformed resource identifiers, unknown resources, missing permissions,
or non-SSE `Accept` headers fail closed with an `event: error` support envelope
instead of opening a successful stream.

## External MCP Servers

External MCP servers are loaded from `config/mcp_servers.yaml` by the Worker
MCP bridge when that bridge is configured. Those external tools are namespaced
as `mcp__<server>__<tool>` to prevent collisions. Worker-consumed external
tools are not automatically exposed through the served `/mcp/tools` registry
unless the server-side registry path is explicitly wired for the running
client.

Before enabling an external server:

1. Review the command, args, and environment in `config/mcp_servers.yaml`.
2. Keep commands list-based; do not depend on shell expansion.
3. Disable servers that are not needed for the current workflow.
4. Restart AM Workbench after changing the config.
5. Recheck `GET /mcp/tools` or the Worker bridge tool listing for the expected
   namespaced tools.

## Troubleshooting

| Symptom | Recovery |
|---|---|
| `401` or guarded response on `/mcp/tools` | Restart the server with `VETINARI_ADMIN_TOKEN` set and send the matching header. |
| `Request body must be a JSON object` | Send a JSON object, not a string, array, number, or empty body. |
| `Unknown tool` | Re-run `tools/list` or `GET /mcp/tools`; use the exact registered name. |
| `missing required argument` | Match the `inputSchema.required` fields returned by the tool list. |
| `MCP_TRANSPORT` on `/mcp/resources/stream` | Send `Accept: text/event-stream` and use an SSE-capable client. |
| `MCP_RESOURCE_URI` on a resource route | Use a URI returned by `GET /mcp/resources`; resource URIs must start with `resource://`. |
| stdio client hangs | Confirm the client sent `initialize` first and that the subprocess command uses the project `.venv312` Python. |
| HTTP transport command exits without serving | This is expected; start `vetinari serve` and call `POST /mcp/message`. |

When filing an MCP support issue, include the transport, command or URL, HTTP
status if applicable, sanitized JSON-RPC method name, tool name, and whether
`GET /mcp/tools` succeeds. Do not include raw prompt content, memory entries,
API keys, or local model paths.
