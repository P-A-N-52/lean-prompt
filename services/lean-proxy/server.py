#!/usr/bin/env python3
"""lean-proxy: keep every upstream MCP tool callable, keep three schemas resident.

A stdio MCP server that fronts other MCP servers. It handshakes with each
upstream named in config.json, asks for its tools/list, and exposes exactly
three meta-tools upward:

    catalog()                upstream tool names + one-line summaries
    describe(name)           that tool's declaration, verbatim
    call(name, arguments)    forward the call, return the result untouched

Nothing is trimmed on the way through: a call's result — content blocks
including image/base64 payloads, structuredContent, isError — reaches the client
as the upstream produced it, and a tool-level error stays a tool-level result
rather than being rewritten into a transport error. Only a *protocol* failure
(unreachable upstream, timeout, JSON-RPC error) becomes a JSON-RPC error here.
The saving is purely resident-prompt size: three compact schemas instead of one
full schema per upstream tool, with the full schemas one describe() away.

Upstreams start concurrently at startup, so a slow or dead one never blocks the
proxy from answering its client. A lost session is marked offline and the next
request reconnects it; a failing call is not itself retried, because a request
that already reached the upstream may have run.
"""

import argparse
import itertools
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import traceback
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "0.1.0"
DEFAULT_CONFIG = Path(__file__).resolve().with_name("config.json")
PREFERRED_PROTOCOL = "2025-06-18"
KNOWN_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")

DEFAULT_TIMEOUT = 60.0
DEFAULT_STARTUP_TIMEOUT = 15.0
DEFAULT_STARTUP_WAIT = 25.0
DEFAULT_GRACE = 5.0
DEFAULT_DESCRIPTION_LIMIT = 120

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TIMEOUT_ERROR = -32001

DEBUG = bool(os.environ.get("LEAN_PROXY_DEBUG"))

META_TOOLS = [
    {
        "name": "catalog",
        "description": "List every tool the upstream MCP servers expose: name, owning "
        "server and a one-line summary. Start here, then describe(name) for a tool's "
        "full schema, call(name, arguments) to run it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "Only list this upstream."}
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "describe",
        "description": "Full description and input schema of one tool from catalog(), "
        "exactly as its upstream declared it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Tool name from catalog()."}
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "call",
        "description": "Run a tool from catalog() with the given arguments and return "
        "the upstream result unchanged (content blocks, structuredContent, isError). "
        "describe() the tool first if you are unsure of its arguments.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "arguments": {
                    "type": "object",
                    "description": "Arguments matching the tool's input schema.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
]

INSTRUCTIONS = (
    "This server proxies other MCP servers with deferred disclosure. catalog() "
    "lists their tools with one-line summaries, describe(name) returns a tool's "
    "full declaration, call(name, arguments) runs it and returns the upstream "
    "result unchanged. No tool was removed: anything the upstreams expose is "
    "reachable through call()."
)


def note(message):
    """Diagnostics go to stderr — stdout carries the JSON-RPC stream and nothing else."""
    sys.stderr.write("[lean-proxy] %s\n" % message)
    sys.stderr.flush()


def debug(message):
    if DEBUG:
        note(message)


def jdump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def summarize(description, limit):
    """Collapse a description to one line of at most `limit` characters.

    The catalog is the resident-cost control for a whole upstream, so a tool
    whose author wrote five paragraphs must not spend them here.
    """
    if not description:
        return ""
    flat = " ".join(str(description).split())
    if len(flat) <= limit:
        return flat
    return flat[: max(limit - 1, 1)].rstrip() + "…"


class ConfigError(Exception):
    """config.json is unusable; the proxy cannot start at all."""


class HandlerError(Exception):
    """A request the proxy refuses, carrying the JSON-RPC error to send back."""

    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code = code
        self.data = data


class UpstreamError(Exception):
    """The proxy could not complete work on behalf of an upstream server."""


class UpstreamOffline(UpstreamError):
    """No usable session: the spawn failed, the process died, or HTTP is unreachable.

    `delivered` says whether the failed request had already reached the upstream.
    Only an undelivered request may be sent again after a reconnect: re-sending one
    that a (possibly still running) upstream already received could run it twice.
    """

    def __init__(self, message, delivered=False):
        super().__init__(message)
        self.delivered = delivered


class UpstreamTimeout(UpstreamError):
    """The upstream took the request but did not answer in time."""


class UpstreamProtocolError(UpstreamError):
    """The upstream answered with a JSON-RPC error or a malformed message."""

    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code = code
        self.data = data


class Config:
    """Upstream definitions plus the knobs read from config.json.

    Both this project's `upstreams` key and Kimi Code's own `mcpServers` shape are
    accepted, so an existing mcp.json can be pasted in as-is.
    """

    def __init__(self, path, raw):
        self.path = path
        self.timeout = _number(raw, "timeout_seconds", DEFAULT_TIMEOUT)
        self.startup_timeout = _number(raw, "startup_timeout_seconds", DEFAULT_STARTUP_TIMEOUT)
        self.startup_wait = _number(raw, "startup_wait_seconds", DEFAULT_STARTUP_WAIT)
        self.grace = _number(raw, "handshake_grace_seconds", DEFAULT_GRACE)
        self.description_limit = int(_number(raw, "description_limit", DEFAULT_DESCRIPTION_LIMIT))
        servers = raw.get("upstreams")
        if servers is None:
            servers = raw.get("mcpServers")
        if not isinstance(servers, dict) or not servers:
            raise ConfigError("%s defines no upstreams ('upstreams' or 'mcpServers')" % path)
        self.upstreams = []
        for name, spec in servers.items():
            if not isinstance(spec, dict):
                raise ConfigError("upstream %r must be an object" % name)
            if spec.get("enabled") is False or spec.get("disabled") is True:
                note("skipping disabled upstream %s" % name)
                continue
            self.upstreams.append(self._normalize(str(name), spec))
        if not self.upstreams:
            raise ConfigError("%s enables no upstreams" % path)

    def _normalize(self, name, spec):
        merged = dict(spec)
        transport = merged.get("transport")
        if not transport:
            transport = "stdio" if merged.get("command") else "http"
        merged["transport"] = transport
        merged.setdefault("timeout_seconds", self.timeout)
        merged.setdefault("startup_timeout_seconds", self.startup_timeout)
        if transport == "stdio" and not merged.get("command"):
            raise ConfigError("stdio upstream %r needs a 'command'" % name)
        if transport == "http" and not merged.get("url"):
            raise ConfigError("http upstream %r needs a 'url'" % name)
        if transport not in ("stdio", "http"):
            raise ConfigError("upstream %r has unknown transport %r" % (name, transport))
        return name, merged


def _number(raw, key, default):
    value = raw.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ConfigError("%s must be a number, got %r" % (key, value))


def load_config(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError("cannot read %s: %s" % (path, exc))
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise ConfigError("%s is not valid JSON: %s" % (path, exc))
    if not isinstance(raw, dict):
        raise ConfigError("%s must contain a JSON object" % path)
    return Config(path, raw)


class Writer:
    """The JSON-RPC stream out: one compact message per line, serialized under a lock."""

    def __init__(self, stream):
        self._stream = stream
        self._lock = threading.Lock()
        self.broken = False

    def send(self, message):
        line = jdump(message) + "\n"
        with self._lock:
            try:
                self._stream.write(line)
                self._stream.flush()
            except (OSError, ValueError) as exc:
                self.broken = True
                debug("cannot write to the client: %s" % exc)


class Upstream:
    """One upstream MCP server: handshake, tool table, call forwarding.

    `tools` survives a lost session on purpose: after an upstream dies its
    declaration is still worth describe()-ing, and catalog() can then say which
    server is down instead of silently dropping its tools from the list.
    """

    transport = "?"

    def __init__(self, name, spec, protocol_version):
        self.name = name
        self.spec = spec
        self.protocol_version = protocol_version
        self.timeout = float(spec.get("timeout_seconds", DEFAULT_TIMEOUT))
        self.startup_timeout = float(spec.get("startup_timeout_seconds", DEFAULT_STARTUP_TIMEOUT))
        self.tools = []
        self.status = "starting"
        self.detail = ""
        self._ids = itertools.count(1)
        self._connect_lock = threading.RLock()
        self._pending = {}
        self._pending_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def connect(self):
        """Handshake if there is no live session. Raises UpstreamError on failure."""
        with self._connect_lock:
            if self.status == "online" and self._alive():
                return
            self._close_session()
            try:
                self._handshake()
            except UpstreamError as exc:
                self._close_session()
                self.status = "offline"
                self.detail = str(exc)
                raise
            except Exception as exc:  # a bug here must not look like a live upstream
                self._close_session()
                self.status = "offline"
                self.detail = "unexpected %s: %s" % (type(exc).__name__, exc)
                debug(traceback.format_exc())
                raise UpstreamError("upstream '%s' is offline: %s" % (self.name, self.detail))
            self.status = "online"
            self.detail = ""
            debug("upstream %s online with %d tools" % (self.name, len(self.tools)))

    def ensure_online(self):
        if self.status == "online" and self._alive():
            return
        self.connect()

    def call_tool(self, tool_name, arguments, timeout=None):
        """Forward tools/call and hand back the upstream's result object untouched."""
        timeout = self.timeout if timeout is None else timeout
        params = {"name": tool_name, "arguments": arguments}
        self.ensure_online()
        try:
            return self._request("tools/call", params, timeout)
        except UpstreamOffline as exc:
            if not exc.delivered:
                # Nothing reached the upstream, so catalog() should stop calling it
                # online — for stdio the reader thread does this, for HTTP nobody else can.
                self.status = "offline"
                self.detail = str(exc)
            if exc.delivered or not self.spec.get("reconnect", True):
                raise
            # The session died between ensure_online() and the write, so the upstream
            # never saw the request: reconnect and send it exactly once.
            debug("%s dropped the connection before the request landed; reconnecting" % self.name)
            self.connect()
            return self._request("tools/call", params, timeout)

    def close(self):
        with self._connect_lock:
            self._close_session()

    def _alive(self):
        return True

    def _handshake(self):
        raise NotImplementedError

    def _close_session(self):
        pass

    def _fail_pending(self, message):
        """Wake every waiter with a transport failure instead of leaving it to time out."""
        with self._pending_lock:
            slots = list(self._pending.values())
            self._pending.clear()
        for slot in slots:
            slot["failure"] = message
            slot["event"].set()

    def _init_params(self):
        return {
            "protocolVersion": self.protocol_version,
            "capabilities": {},
            "clientInfo": {"name": "lean-proxy", "version": VERSION},
        }

    def _adopt_tools(self, result):
        listed = result.get("tools") if isinstance(result, dict) else None
        self.tools = [t for t in (listed or []) if isinstance(t, dict) and t.get("name")]

    def _request(self, method, params, timeout):
        raise NotImplementedError

    def _notify(self, method, params=None):
        raise NotImplementedError


class StdioUpstream(Upstream):
    """An upstream the proxy spawns and talks to over stdin/stdout."""

    transport = "stdio"

    def __init__(self, name, spec, protocol_version):
        super().__init__(name, spec, protocol_version)
        self.proc = None
        self._write_lock = threading.Lock()
        self._stderr_tail = []

    def _alive(self):
        return self.proc is not None and self.proc.poll() is None

    def _handshake(self):
        command = self.spec["command"]
        argv = [str(command)] + [str(a) for a in (self.spec.get("args") or [])]
        env = dict(os.environ)
        for key, value in (self.spec.get("env") or {}).items():
            env[str(key)] = str(value)
        cwd = self.spec.get("cwd") or None
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            raise UpstreamOffline(
                "upstream '%s' is offline: cannot start %s: %s" % (self.name, command, exc)
            )
        self.proc = proc
        self._stderr_tail = []
        threading.Thread(
            target=self._drain_stderr, args=(proc,), daemon=True, name="lp-%s-err" % self.name
        ).start()
        threading.Thread(
            target=self._read_loop, args=(proc,), daemon=True, name="lp-%s-in" % self.name
        ).start()
        self._request("initialize", self._init_params(), self.startup_timeout)
        self._notify("notifications/initialized")
        self._adopt_tools(self._request("tools/list", {}, self.startup_timeout))

    def _close_session(self):
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        except OSError:
            pass
        self._fail_pending("upstream '%s' is offline: session closed" % self.name)

    def close(self):
        with self._connect_lock:
            self._close_session()
            if self.status != "offline":
                self.status = "closed"

    # -- transport ---------------------------------------------------------

    def _write(self, message):
        line = jdump(message) + "\n"
        with self._write_lock:
            proc = self.proc
            if proc is None or proc.stdin is None:
                raise UpstreamOffline("upstream '%s' is offline: no session" % self.name)
            proc.stdin.write(line)
            proc.stdin.flush()

    def _request(self, method, params, timeout):
        proc = self.proc
        if proc is None or proc.stdin is None:
            raise UpstreamOffline("upstream '%s' is offline: no session" % self.name)
        if proc.poll() is not None:
            raise UpstreamOffline(
                "upstream '%s' is offline: %s" % (self.name, self._exit_reason(proc))
            )
        rid = next(self._ids)
        slot = {"event": threading.Event(), "msg": None, "failure": None}
        with self._pending_lock:
            self._pending[rid] = slot
        try:
            self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        except OSError as exc:
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise UpstreamOffline("upstream '%s' is offline: %s" % (self.name, exc))
        if not slot["event"].wait(timeout):
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise UpstreamTimeout(
                "upstream '%s' did not answer %s within %.0fs" % (self.name, method, timeout)
            )
        if slot["failure"]:
            raise UpstreamOffline(slot["failure"], delivered=True)
        return _unwrap(slot["msg"] or {}, self.name, method)

    def _notify(self, method, params=None):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        try:
            self._write(message)
        except OSError:
            pass

    def _read_loop(self, proc):
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    note("%s sent a non-JSON line: %r" % (self.name, line[:120]))
                    continue
                self._dispatch(message)
        except (OSError, ValueError) as exc:
            debug("%s reader stopped: %s" % (self.name, exc))
        finally:
            if self.proc is proc:
                reason = self._exit_reason(proc)
                self.status = "offline"
                self.detail = reason
                note("upstream %s went offline: %s" % (self.name, reason))
                self._fail_pending("upstream '%s' is offline: %s" % (self.name, reason))

    def _dispatch(self, message):
        if isinstance(message, list):
            for item in message:
                self._dispatch(item)
            return
        if not isinstance(message, dict):
            return
        rid = message.get("id")
        if rid is not None and ("result" in message or "error" in message):
            with self._pending_lock:
                slot = self._pending.pop(rid, None)
            if slot is not None:
                slot["msg"] = message
                slot["event"].set()
            return
        method = message.get("method")
        if method is None:
            return
        if rid is not None:
            # A server-to-client request (sampling, roots, elicitation): this proxy
            # implements none of them, and leaving the upstream waiting is worse.
            self._notify_reply(rid, method)
            return
        if method == "notifications/tools/list_changed":
            debug("%s announced a tool change" % self.name)
            try:
                self.refresh_tools()
            except UpstreamError as exc:
                debug("%s tool refresh failed: %s" % (self.name, exc))

    def refresh_tools(self):
        self._adopt_tools(self._request("tools/list", {}, self.startup_timeout))

    def _notify_reply(self, rid, method):
        try:
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {
                        "code": METHOD_NOT_FOUND,
                        "message": "lean-proxy does not implement %s" % method,
                    },
                }
            )
        except (OSError, UpstreamError):
            pass

    def _drain_stderr(self, proc):
        try:
            for line in proc.stderr:
                line = line.rstrip()
                if not line:
                    continue
                self._stderr_tail.append(line)
                del self._stderr_tail[:-10]
                debug("%s: %s" % (self.name, line))
        except (OSError, ValueError):
            pass

    def _exit_reason(self, proc):
        code = proc.poll()
        reason = "process exited (code %s)" % code
        if self._stderr_tail:
            reason += "; last stderr: %s" % self._stderr_tail[-1][:200]
        return reason


class HttpUpstream(Upstream):
    """An upstream reached over MCP streamable HTTP.

    A single POST carries one request; the answer comes back either as a JSON
    body or as a one-message SSE stream, and the session id the server hands out
    on initialize is replayed on every later request.
    """

    transport = "http"

    def __init__(self, name, spec, protocol_version):
        super().__init__(name, spec, protocol_version)
        self.session_id = None

    def _close_session(self):
        self.session_id = None
        self._fail_pending("upstream '%s' is offline: session closed" % self.name)

    def _handshake(self):
        self.session_id = None
        self._request("initialize", self._init_params(), self.startup_timeout)
        self._notify("notifications/initialized")
        self._adopt_tools(self._request("tools/list", {}, self.startup_timeout))

    def _request(self, method, params, timeout):
        rid = next(self._ids)
        message = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        body, content_type = self._post(message, timeout)
        return _unwrap(self._answer(body, content_type, rid), self.name, method)

    def _notify(self, method, params=None):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        try:
            self._post(message, self.startup_timeout)
        except UpstreamError as exc:
            debug("%s notification %s failed: %s" % (self.name, method, exc))

    def _post(self, message, timeout):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.protocol_version,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(
            self.spec["url"], data=jdump(message).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id:
                    self.session_id = session_id
                return response.read().decode("utf-8", "replace"), (
                    response.headers.get("Content-Type") or ""
                ).lower()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except (OSError, ValueError):
                pass
            if exc.code == 404 and self.session_id:
                self.session_id = None
            raise UpstreamProtocolError(
                exc.code, "upstream '%s' answered HTTP %s: %s" % (self.name, exc.code, detail)
            )
        except socket.timeout:
            raise UpstreamTimeout(
                "upstream '%s' did not answer within %.0fs" % (self.name, timeout)
            )
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, socket.timeout):
                raise UpstreamTimeout(
                    "upstream '%s' did not answer within %.0fs" % (self.name, timeout)
                )
            raise UpstreamOffline("upstream '%s' is unreachable: %s" % (self.name, exc.reason))
        except OSError as exc:
            raise UpstreamOffline("upstream '%s' is unreachable: %s" % (self.name, exc))

    def _answer(self, body, content_type, rid):
        if "text/event-stream" in content_type or body.lstrip().startswith(("event:", "data:")):
            for line in body.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    message = json.loads(chunk)
                except ValueError:
                    continue
                if isinstance(message, dict) and message.get("id") == rid:
                    return message
            raise UpstreamProtocolError(
                None, "upstream '%s' sent no response for id %s" % (self.name, rid)
            )
        try:
            message = json.loads(body)
        except ValueError:
            raise UpstreamProtocolError(
                None,
                "upstream '%s' returned a non-JSON body: %r" % (self.name, body[:200]),
            )
        if isinstance(message, list):
            for item in message:
                if isinstance(item, dict) and item.get("id") == rid:
                    return item
            raise UpstreamProtocolError(
                None, "upstream '%s' sent no response for id %s" % (self.name, rid)
            )
        if not isinstance(message, dict):
            raise UpstreamProtocolError(
                None, "upstream '%s' returned an unexpected body" % self.name
            )
        return message


def _unwrap(message, upstream_name, method):
    """Turn an upstream JSON-RPC message into its result, or raise the failure."""
    if "error" in message:
        error = message["error"] or {}
        raise UpstreamProtocolError(
            error.get("code"),
            "upstream '%s' returned error %s for %s: %s"
            % (upstream_name, error.get("code"), method, error.get("message")),
            error.get("data"),
        )
    if "result" not in message:
        raise UpstreamProtocolError(
            None, "upstream '%s' answered %s without a result" % (upstream_name, method)
        )
    return message["result"]


class Proxy:
    """The MCP server the client sees: three meta-tools over a table of upstreams."""

    def __init__(self, config):
        self.config = config
        self.upstreams = []
        for name, spec in config.upstreams:
            cls = StdioUpstream if spec["transport"] == "stdio" else HttpUpstream
            self.upstreams.append(cls(name, spec, PREFERRED_PROTOCOL))
        self.by_name = {u.name: u for u in self.upstreams}
        self.protocol_version = PREFERRED_PROTOCOL
        self._index = {}
        self._alias = {}
        self._owners = {}
        self._client_ready = threading.Event()
        self._startup_done = threading.Event()
        self._threads = set()
        self._threads_lock = threading.Lock()

    # -- startup -----------------------------------------------------------

    def start(self):
        """Handshake with every upstream in the background, then index them."""
        threading.Thread(target=self._startup, daemon=True, name="lp-startup").start()

    def _startup(self):
        if not self.upstreams:
            self._startup_done.set()
            return
        # The client's initialize names the protocol version to offer upstreams, and
        # it lands within milliseconds of the process starting; waiting briefly for it
        # is cheaper than negotiating twice.
        self._client_ready.wait(self.config.grace)
        for upstream in self.upstreams:
            upstream.protocol_version = self.protocol_version
        threads = [
            threading.Thread(target=self._connect_one, args=(u,), daemon=True, name="lp-%s" % u.name)
            for u in self.upstreams
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.reindex()
        self._startup_done.set()

    def _connect_one(self, upstream):
        try:
            upstream.connect()
        except UpstreamError as exc:
            note("upstream %s is offline: %s" % (upstream.name, exc))

    def note_client_protocol(self, requested):
        """Record the version the client asked for and let startup proceed."""
        if isinstance(requested, str) and requested in KNOWN_PROTOCOLS:
            self.protocol_version = requested
        self._client_ready.set()

    def wait_ready(self):
        """Block until the startup pass is over, bounded by startup_wait_seconds."""
        if not self._startup_done.is_set():
            self._startup_done.wait(self.config.startup_wait)

    def close(self):
        for upstream in self.upstreams:
            upstream.close()
        self._client_ready.set()
        self._startup_done.set()

    # -- tool index --------------------------------------------------------

    def reindex(self):
        """Rebuild name -> upstream tool, prefixing names that two upstreams share.

        A bare name stays addressable while it is unambiguous; two upstreams
        offering `echo` both move to `server__echo`, which is also accepted as an
        alias for every other tool.
        """
        index, alias, owners = {}, {}, {}
        for upstream in self.upstreams:
            for tool in upstream.tools:
                owners.setdefault(tool["name"], []).append(upstream)
        for upstream in self.upstreams:
            for tool in upstream.tools:
                bare = tool["name"]
                canonical = bare if len(owners[bare]) == 1 else "%s__%s" % (upstream.name, bare)
                index[canonical] = (upstream, tool)
        for upstream in self.upstreams:
            for tool in upstream.tools:
                qualified = "%s__%s" % (upstream.name, tool["name"])
                if qualified not in index:
                    alias.setdefault(qualified, tool["name"])
        self._index = index
        self._alias = alias
        self._owners = owners

    def canonical_name(self, tool):
        bare = tool["name"]
        if len(self._owners.get(bare, ())) == 1:
            return bare
        for upstream, candidate in self._index.values():
            if candidate is tool:
                return "%s__%s" % (upstream.name, bare)
        return bare

    def resolve(self, name):
        """Look up a client-supplied name, waiting out the startup pass on first use."""
        self.wait_ready()
        self.reindex()
        entry = self._index.get(name)
        if entry is not None:
            return name, entry
        canonical = self._alias.get(name)
        if canonical is not None:
            entry = self._index.get(canonical)
            if entry is not None:
                return canonical, entry
        return name, None

    def _unknown_tool(self, name, verb):
        available = sorted(self._index)
        shown = available[:40]
        listed = ", ".join(shown) if shown else "none — catalog() shows which upstreams answered"
        if len(available) > len(shown):
            listed += ", …"
        raise HandlerError(
            INVALID_PARAMS,
            "unknown tool %r; %s() needs a name from catalog(). Available: %s"
            % (name, verb, listed),
            {"available": shown, "tool_count": len(available)},
        )

    # -- meta-tools --------------------------------------------------------

    def catalog(self, params):
        server_filter = params.get("server")
        if server_filter is not None and not isinstance(server_filter, str):
            raise HandlerError(INVALID_PARAMS, "server must be a string")
        if server_filter is not None and server_filter not in self.by_name:
            known = ", ".join(sorted(self.by_name))
            raise HandlerError(
                INVALID_PARAMS,
                "unknown upstream %r. Configured servers: %s" % (server_filter, known),
                {"servers": sorted(self.by_name)},
            )
        self.wait_ready()
        self.reindex()

        limit = self.config.description_limit
        lines = []
        servers = []
        tools = []
        for upstream in self.upstreams:
            if server_filter and upstream.name != server_filter:
                continue
            live = upstream.status == "online"
            servers.append(
                {
                    "name": upstream.name,
                    "transport": upstream.transport,
                    "status": upstream.status,
                    "detail": upstream.detail,
                    "tool_count": len(upstream.tools),
                }
            )
            lines.append(
                "%s [%s] %s%s"
                % (
                    upstream.name,
                    upstream.transport,
                    "online" if live else upstream.status.upper(),
                    "" if live else " — " + (upstream.detail or "not reachable"),
                )
            )
            entries = sorted((self.canonical_name(t), t) for t in upstream.tools)
            if not entries:
                lines.append("  (no tools)")
            for name, tool in entries:
                summary = summarize(tool.get("description"), limit)
                lines.append("  %-26s %s" % (name, summary))
                tools.append(
                    {
                        "name": name,
                        "server": upstream.name,
                        "summary": summary,
                        "description_chars": len(tool.get("description") or ""),
                    }
                )
        text = "\n".join(lines)
        text += "\n(describe(name) for a full schema; call(name, arguments) to run one.)"
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": {"servers": servers, "tools": tools},
            "isError": False,
        }

    def describe(self, params):
        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            raise HandlerError(INVALID_PARAMS, "describe() needs the tool name from catalog()")
        canonical, entry = self.resolve(name.strip())
        if entry is None:
            self._unknown_tool(name, "describe")
        upstream, tool = entry
        definition = dict(tool)
        definition["name"] = canonical
        definition["server"] = upstream.name
        definition["upstream_status"] = upstream.status
        return {
            "content": [{"type": "text", "text": json.dumps(definition, ensure_ascii=False, indent=2)}],
            "structuredContent": definition,
            "isError": False,
        }

    def call(self, params):
        name = params.get("name")
        arguments = params.get("arguments")
        if not isinstance(name, str) or not name.strip():
            raise HandlerError(INVALID_PARAMS, "call() needs the tool name from catalog()")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise HandlerError(INVALID_PARAMS, "arguments must be an object")
        canonical, entry = self.resolve(name.strip())
        if entry is None:
            self._unknown_tool(name, "call")
        upstream, tool = entry
        try:
            return upstream.call_tool(tool["name"], arguments)
        except UpstreamTimeout as exc:
            raise HandlerError(
                TIMEOUT_ERROR,
                str(exc),
                {"reason": "timeout", "upstream": upstream.name, "tool": canonical},
            )
        except UpstreamProtocolError as exc:
            raise HandlerError(
                INTERNAL_ERROR,
                str(exc),
                {
                    "reason": "upstream_error",
                    "upstream": upstream.name,
                    "tool": canonical,
                    "upstream_code": exc.code,
                    "upstream_data": exc.data,
                },
            )
        except UpstreamError as exc:
            raise HandlerError(
                INTERNAL_ERROR,
                str(exc),
                {"reason": "offline", "upstream": upstream.name, "tool": canonical},
            )

    # -- MCP surface -------------------------------------------------------

    def dispatch(self, method, params):
        """Return the result for a request, or raise HandlerError."""
        if method == "initialize":
            self.note_client_protocol(params.get("protocolVersion"))
            return {
                "protocolVersion": self.protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "lean-proxy", "version": VERSION},
                "instructions": INSTRUCTIONS,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": META_TOOLS}
        if method == "tools/call":
            return self._meta_call(params)
        if method == "resources/list":
            return {"resources": []}
        if method == "resources/templates/list":
            return {"resourceTemplates": []}
        if method == "prompts/list":
            return {"prompts": []}
        raise HandlerError(METHOD_NOT_FOUND, "lean-proxy does not implement %s" % method)

    def _meta_call(self, params):
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise HandlerError(INVALID_PARAMS, "arguments must be an object")
        if name == "catalog":
            return self.catalog(arguments)
        if name == "describe":
            return self.describe(arguments)
        if name == "call":
            return self.call(arguments)
        raise HandlerError(
            INVALID_PARAMS,
            "unknown tool %r; this server exposes catalog, describe and call" % (name,),
            {"available": [t["name"] for t in META_TOOLS]},
        )

    def notify(self, method, params):
        if method == "notifications/initialized":
            self._client_ready.set()
        elif method == "notifications/cancelled":
            debug("client cancelled %s" % (params or {}).get("requestId"))

    def handle_one(self, message):
        """Turn one decoded client message into the response to send, or None."""
        if not isinstance(message, dict):
            return _error(None, INVALID_REQUEST, "messages must be JSON objects")
        rid = message.get("id")
        method = message.get("method")
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        if not isinstance(method, str):
            if rid is None:
                return None
            return _error(rid, INVALID_REQUEST, "message has no method")
        if rid is None:
            self.notify(method, params)
            return None
        try:
            result = self.dispatch(method, params)
        except HandlerError as exc:
            return _error(rid, exc.code, str(exc), exc.data)
        except Exception as exc:
            debug(traceback.format_exc())
            return _error(rid, INTERNAL_ERROR, "lean-proxy failed on %s: %s" % (method, exc))
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    # -- serving -----------------------------------------------------------

    def serve(self, writer):
        """Read the JSON-RPC stream, answering concurrently so one slow upstream
        cannot stall the proxy for its other servers."""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                writer.send(_error(None, PARSE_ERROR, "invalid JSON"))
                continue
            if isinstance(message, list):
                responses = [r for r in (self.handle_one(m) for m in message) if r]
                if responses:
                    writer.send(responses)
                continue
            thread = threading.Thread(target=self._answer_one, args=(message, writer), daemon=True)
            with self._threads_lock:
                self._threads = {t for t in self._threads if t.is_alive()}
                self._threads.add(thread)
            thread.start()

    def _answer_one(self, message, writer):
        response = self.handle_one(message)
        if response is not None and not writer.broken:
            writer.send(response)

    def drain(self, timeout=5.0):
        with self._threads_lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout)


def _error(rid, code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": error}


def probe(proxy, as_json):
    """--probe: handshake, print the catalog as the model would get it, exit."""
    proxy.note_client_protocol(None)
    proxy.wait_ready()
    catalogue = proxy.catalog({})
    if as_json:
        print(json.dumps(catalogue["structuredContent"], ensure_ascii=False, indent=2))
    else:
        print(catalogue["content"][0]["text"])
    offline = [u.name for u in proxy.upstreams if u.status != "online"]
    if offline:
        note("offline upstreams: %s" % ", ".join(offline))
    return 1 if offline else 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="lean-proxy", description="stdio MCP proxy with deferred tool disclosure"
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="upstream definitions (JSON)")
    parser.add_argument(
        "--probe", action="store_true", help="print the catalog and exit (no MCP loop)"
    )
    parser.add_argument("--json", action="store_true", help="with --probe, print JSON")
    args = parser.parse_args(argv)

    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        note("error: %s" % exc)
        return 2

    proxy = Proxy(config)
    proxy.start()
    if args.probe:
        try:
            return probe(proxy, args.json)
        finally:
            proxy.close()

    _install_signal_handlers(proxy)
    writer = Writer(sys.stdout)
    try:
        proxy.serve(writer)
    except KeyboardInterrupt:
        pass
    finally:
        proxy.drain()
        proxy.close()
    return 0


def _install_signal_handlers(proxy):
    """Take the upstream processes down with us instead of orphaning them.

    A client that closes stdin ends the read loop cleanly; one that signals instead
    would otherwise leave every upstream running with no way to reach it.
    """

    def handler(signum, _frame):
        note("signal %d: closing %d upstreams" % (signum, len(proxy.upstreams)))
        proxy.close()
        raise SystemExit(0)

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(signum, handler)
        except (ValueError, OSError, AttributeError):
            pass


if __name__ == "__main__":
    sys.exit(main())
