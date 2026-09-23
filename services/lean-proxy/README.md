# lean-proxy

An MCP proxy that keeps **every** upstream tool callable while keeping three
compact schemas resident. It answers the question the rest of this plugin works
around: instead of removing MCP tools from the main agent, or delegating them to
a sub-agent, front the MCP servers with a server that exposes the tool *index*
and fetches a declaration only when something needs it.

```text
                     ┌─ lean-proxy ─────────────────────────────┐
  main agent ──stdio─┤ catalog() | describe(name) | call(n,a)    ├─stdio/http─┬─ kimi-cu
                     └──────────────────────────────────────────┘            ├─ desktop-browser
                                                                             └─ anything else
```

Three meta-tools, and the upstream's own tools behind them:

| meta-tool | what it does |
|---|---|
| `catalog()` | every upstream tool name + a one-line summary (truncated to `description_limit`, 120 chars by default) |
| `describe(name)` | that tool's full description and input schema, exactly as its upstream declared it |
| `call(name, arguments)` | forwards the call and returns the upstream result unchanged |

Nothing was removed: `call()` reaches every tool the upstreams expose, so this
is compatible with any MCP server, including ones with side effects that a
sub-agent cannot run (session state) — the model just looks the tool up first.

## Measured

The resident cost is the three meta-tool schemas plus the `initialize`
instructions. On this machine, against real upstreams:

| upstream | tools | schemas resident before | through lean-proxy | saving |
|---|---:|---:|---:|---:|
| kimi-cu 0.5.8 (10 tools) | 10 | 8,021 chars | **1,424 chars** | **−82%** |
| the 9-tool test fixture | 9 | 2,257 chars | 1,424 chars | −37% |

Chars are utf-8 compact JSON (`json.dumps(..., ensure_ascii=False,
separators=(',', ':'))`), the unit `prompt_audit.py` uses; the proxy's own wire
format is the same. Reproduce with `tests/check_kimi_cu.py` — it prints the
table. The saving grows with each upstream added, because a whole server's
schemas stop being resident and only its catalog lines remain.

## Wiring it into Kimi Code

Add the proxy to `mcp.json` (user scope shown; project scope works too), then
new-session or `/reload`:

```json
{
  "mcpServers": {
    "lean": {
      "command": "python3",
      "args": ["/Users/pan/Desktop/kimi-code-prompt/services/lean-proxy/server.py"]
    }
  }
}
```

Then **remove the direct servers** from `mcp.json` — they are now upstreams of
the proxy, and leaving both in place pays for the schemas twice. The upstreams
move into the proxy's own `config.json`:

```json
{
  "timeout_seconds": 60,
  "startup_timeout_seconds": 15,
  "startup_wait_seconds": 25,
  "description_limit": 120,
  "upstreams": {
    "kimi-cu": {
      "command": "/Applications/KimiCU.app/Contents/MacOS/kimi-cu",
      "args": ["mcp", "-s", "user"]
    },
    "browser": { "url": "http://127.0.0.1:8931/mcp", "transport": "http" }
  }
}
```

Notes on the shape:

- `upstreams` and an `mcpServers` key are both accepted, so an existing
  `~/.kimi-code/mcp.json` can be pasted in as-is.
- Transport is inferred: `command` → stdio, `url` → http. `transport` overrides
  it.
- Per-upstream overrides: `timeout_seconds`, `startup_timeout_seconds`, `cwd`,
  `env`, `reconnect`.
- `description_limit` is the catalog's one-line budget per tool.
- `startup_timeout_seconds` is the handshake budget; `startup_wait_seconds` is
  how long the first `catalog()`/`describe()`/`call()` waits for startup to
  finish before reporting upstreams as still starting.
- `reconnect: false` leaves a dropped upstream dead until the proxy restarts
  (the default reconnects on the next request).

Debug one config without wiring it in — this handshakes with every upstream,
prints the catalog exactly as the model would receive it, and exits 1 if any
upstream is offline:

```sh
python3 services/lean-proxy/server.py --probe
python3 services/lean-proxy/server.py --probe --json   # structured catalog
```

## Fidelity

`call()` returns the upstream's `result` object untouched:

- content blocks pass through whole, including `image` blocks with their base64
  payload (`data`, `mimeType`), and any block type a future upstream adds;
- `structuredContent` and `isError` are preserved, so a **tool-level** error
  stays a tool result (the model reads it as data);
- extra declaration fields — `title`, `annotations`, `outputSchema` — survive
  `describe()`.

The envelope around the result is rebuilt, because the JSON-RPC `id` belongs to
this connection, not the upstream's. Values, key order and string escaping are
not touched: `test_proxy.py` extracts the raw JSON text of the `result` from a
direct call and from the same call through the proxy and asserts the two strings
are equal, for a text result, an image/base64 result, a `structuredContent`
result, a non-ASCII result, a 64 KiB result and an `isError` result. That
comparison is the strongest statement available short of splicing bytes.

A **protocol** failure is different and does become a JSON-RPC error here:

| situation | code | `error.data.reason` |
|---|---|---|
| unknown tool name (`describe`/`call`) | `-32602` | — (the message lists the available names) |
| upstream answered with a JSON-RPC error | `-32603` | `upstream_error` (+ `upstream_code`, `upstream_data`) |
| upstream unreachable / process gone | `-32603` | `offline` |
| upstream did not answer in time | `-32001` | `timeout` |

A tool that is only *slow* is never failed early: the timeout is per upstream
(`timeout_seconds`, default 60).

## Behaviour worth knowing

- **Startup is concurrent.** Upstreams are handshaken in parallel threads and a
  slow or dead one never blocks the proxy from answering. The first `catalog()`
  waits for the pass to settle (bounded by `startup_wait_seconds`); after that,
  every request is answered from the cached tool table.
- **A dead upstream keeps its declarations.** `catalog()` marks it `OFFLINE`
  with the reason (exit code, last stderr line, HTTP status), `describe()` still
  returns its tools, and `call()` fails with the reason instead of a bare error.
- **A dropped session is reconnected on the next request** rather than retried
  inside the failing call: a request that already reached the upstream may have
  run, so re-sending it could execute a side effect twice. The one exception is
  a request that never landed (the write failed) — that one is reconnected and
  sent once.
- **Name collisions get a server prefix.** A name two upstreams share becomes
  `server__tool` for both; unique names stay bare, and `server__tool` is accepted
  as an alias everywhere.
- **Requests are answered concurrently**, so one slow upstream does not stall the
  proxy for the others. Responses carry their own `id`, and JSON-RPC permits
  out-of-order replies.
- **stdout carries JSON-RPC only.** Diagnostics go to stderr; set
  `LEAN_PROXY_DEBUG=1` for the verbose stream (upstream stderr, tool changes).

## Tests

Both suites are stdlib-only and need no test framework.

```sh
python3 services/lean-proxy/tests/test_proxy.py        # 18 hermetic checks, ~5s
python3 services/lean-proxy/tests/test_proxy.py -v     # tracebacks
python3 services/lean-proxy/tests/check_kimi_cu.py     # real kimi-cu (skips if absent)
```

`test_proxy.py` runs the proxy against local fixtures (`tests/fixture_core.py`)
over both transports and covers: the three-tool surface and its resident cost;
catalog truncation and the `server` filter; verbatim `describe`; byte-exact
`call` for six result shapes; tool-level `isError`; upstream protocol errors;
timeouts; unknown names; name collision prefixing; an unstartable upstream; a
disabled upstream; a died-and-unrecoverable upstream; recovery after a drop; an
upstream death taking its children with it; HTTP in both `application/json` and
SSE framing, including an HTTP upstream that goes away mid-session; and stdout
hygiene.

The HTTP fixture is deliberately strict — it rejects any request that omits the
session id it handed out on initialize, or the `MCP-Protocol-Version` header — so
a passing run also means the proxy's HTTP client half sends what the spec
requires.
