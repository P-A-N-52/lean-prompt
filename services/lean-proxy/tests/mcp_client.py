#!/usr/bin/env python3
"""A minimal stdio MCP client, and the connection helper the tests build on.

Keeps the *raw* line of every response: the passthrough checks compare the raw
text of a `result` in a direct upstream answer with the same text on its way back
through the proxy, which needs the bytes, not just the parsed object.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.dirname(HERE)
SERVER = os.path.join(SERVICES, "server.py")
FIXTURE_STDIO = os.path.join(HERE, "fixture_stdio_mcp.py")
FIXTURE_HTTP = os.path.join(HERE, "fixture_http_mcp.py")
COMPACT = {"ensure_ascii": False, "separators": (",", ":")}

sys.path.insert(0, HERE)

from fixture_core import handle as fixture_handle  # noqa: E402


def compact(value):
    return json.dumps(value, **COMPACT)


class ClientError(Exception):
    pass


class Client:
    """One MCP connection over stdio: initialize, tools/list, tools/call."""

    def __init__(self, command, cwd=None, env=None, protocol="2025-06-18"):
        merged = dict(os.environ)
        merged.update(env or {})
        self.protocol = protocol
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=merged,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._ids = 0
        self._lock = threading.Lock()
        self._slots = {}
        self._slots_lock = threading.Lock()
        self.stderr = []
        self.raw_responses = {}
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    # -- plumbing ----------------------------------------------------------

    def _read_loop(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    self.stderr.append("non-JSON on stdout: %r" % line[:200])
                    continue
                if not isinstance(message, dict):
                    continue
                rid = message.get("id")
                if rid is None:
                    continue
                with self._slots_lock:
                    slot = self._slots.pop(rid, None)
                if slot is not None:
                    slot["raw"] = line
                    slot["msg"] = message
                    slot["event"].set()
        except (OSError, ValueError):
            pass

    def _drain_stderr(self):
        try:
            for line in self.proc.stderr:
                self.stderr.append(line.rstrip())
        except (OSError, ValueError):
            pass

    def close(self):
        if self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        try:
            self.proc.stdout.close()
            self.proc.stderr.close()
        except OSError:
            pass

    def send_raw(self, text):
        with self._lock:
            self.proc.stdin.write(text + "\n")
            self.proc.stdin.flush()

    def notify(self, method, params=None):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.send_raw(compact(message))

    def request(self, method, params=None, timeout=30):
        with self._lock:
            self._ids += 1
            rid = self._ids
        slot = {"event": threading.Event(), "msg": None, "raw": None}
        with self._slots_lock:
            self._slots[rid] = slot
        message = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            message["params"] = params
        self.send_raw(compact(message))
        if not slot["event"].wait(timeout):
            with self._slots_lock:
                self._slots.pop(rid, None)
            raise ClientError("no answer to %s within %ss" % (method, timeout))
        self.raw_responses[rid] = slot["raw"]
        return slot

    def result(self, method, params=None, timeout=30):
        slot = self.request(method, params, timeout)
        if "error" in slot["msg"]:
            raise ClientError("%s failed: %s" % (method, slot["msg"]["error"]))
        return slot["msg"]["result"]

    def error(self, method, params=None, timeout=30):
        """Return the JSON-RPC error object of a request that must fail."""
        slot = self.request(method, params, timeout)
        if "error" not in slot["msg"]:
            raise AssertionError("%s unexpectedly succeeded: %s" % (method, slot["msg"]))
        return slot["msg"]["error"]

    # -- MCP surface -------------------------------------------------------

    def initialize(self, timeout=30):
        result = self.result(
            "initialize",
            {
                "protocolVersion": self.protocol,
                "capabilities": {},
                "clientInfo": {"name": "lean-proxy-tests", "version": "0.1.0"},
            },
            timeout,
        )
        self.initialize_result = result
        self.notify("notifications/initialized")
        return result

    def tools(self, timeout=30):
        return self.result("tools/list", {}, timeout)["tools"]

    def call(self, name, arguments=None, timeout=60):
        """Raw slot for a tools/call, so both the parsed result and raw text survive."""
        params = {"name": name}
        if arguments is not None:
            params["arguments"] = arguments
        return self.request("tools/call", params, timeout)

    def call_result(self, name, arguments=None, timeout=60):
        slot = self.call(name, arguments, timeout)
        if "result" not in slot["msg"]:
            raise ClientError("%s failed: %s" % (name, slot["msg"].get("error")))
        return slot["msg"]["result"]


def meta(client, name, arguments=None, timeout=60):
    """Call a lean-proxy meta-tool; raises with the proxy's message on error."""
    return client.call_result(name, arguments, timeout)


def raw_result_text(raw_line):
    """The raw JSON text of the top-level `result` value in a JSON-RPC line.

    Compared between a direct call and the same call through the proxy: identical
    text means the result crossed the proxy with its values, its key order and its
    string escaping untouched.
    """
    fields = top_level_fields(raw_line)
    if "result" not in fields:
        raise AssertionError("no result in %r" % raw_line[:200])
    return fields["result"]


def top_level_fields(text):
    """{key: raw source text of the value} for one JSON object, escape-aware."""
    fields = {}
    index = _skip_ws(text, 0)
    if text[index] != "{":
        raise AssertionError("not a JSON object: %r" % text[:120])
    index += 1
    while True:
        index = _skip_ws(text, index)
        if text[index] == "}":
            return fields
        start = index
        index = _skip_value(text, start)
        key = json.loads(text[start:index])
        index = _skip_ws(text, index)
        if text[index] != ":":
            raise AssertionError("expected ':' at %d in %r" % (index, text[:200]))
        index = _skip_ws(text, index + 1)
        value_start = index
        index = _skip_value(text, value_start)
        fields[key] = text[value_start:index]
        index = _skip_ws(text, index)
        if text[index] == ",":
            index += 1
            continue
        if text[index] == "}":
            return fields
        raise AssertionError("unexpected %r at %d" % (text[index], index))


def _skip_ws(text, index):
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    return index


def _skip_value(text, index):
    char = text[index]
    if char == '"':
        index += 1
        while index < len(text):
            if text[index] == "\\":
                index += 2
                continue
            if text[index] == '"':
                return index + 1
            index += 1
        raise AssertionError("unterminated string in %r" % text[:200])
    if char in "{[":
        depth = 0
        while index < len(text):
            current = text[index]
            if current == '"':
                index = _skip_value(text, index)
                continue
            if current in "{[":
                depth += 1
            elif current in "}]":
                depth -= 1
                if depth == 0:
                    return index + 1
            index += 1
        raise AssertionError("unterminated container in %r" % text[:200])
    while index < len(text) and text[index] not in ",}] \t\r\n":
        index += 1
    return index


class HttpFixture:
    """The HTTP fixture on an ephemeral port, started directly by the tests."""

    def __init__(self, mode="sse", label=""):
        self.proc = subprocess.Popen(
            [sys.executable, FIXTURE_HTTP, "--port", "0", "--mode", mode, "--label", label],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        line = self.proc.stdout.readline().strip()
        match = re.match(r"PORT=(\d+)$", line)
        if not match:
            self.proc.kill()
            raise ClientError("HTTP fixture did not report a port: %r" % line)
        self.port = int(match.group(1))
        self.url = "http://127.0.0.1:%d/mcp" % self.port

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        try:
            self.proc.stdout.close()
            self.proc.stderr.close()
        except OSError:
            pass


class ExpiringSessionHttpFixture:
    """An HTTP MCP fixture whose session can be terminated on command.

    `fixture_http_mcp.py` stays a cooperative server with one always-valid session.
    This one exists for the streamable-HTTP rule the proxy has to survive: a request
    carrying a terminated session id is answered 404, which per spec tells the client
    to send a fresh initialize. `initializes` counts how often the client
    re-established the session and `dispatched` records the tool calls that actually
    reached a tool, so a test can tell recovery from a silent re-run.
    """

    def __init__(self, reject_sessions=False):
        self._lock = threading.Lock()
        self.session_id = None
        self.session_seq = 0
        self.initializes = 0
        self.dispatched = []
        self.reject_sessions = reject_sessions
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _expiring_handler(self))
        self.url = "http://127.0.0.1:%d/mcp" % self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def expire(self):
        """Terminate the session: later requests carrying its id are answered 404."""
        with self._lock:
            self.session_id = None

    def close(self):
        self._server.shutdown()
        self._server.server_close()


def _expiring_handler(owner):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                message = json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError:
                self._send(400, compact({"error": "body is not JSON"}).encode("utf-8"))
                return
            method = message.get("method")
            if method == "initialize":
                with owner._lock:
                    owner.session_seq += 1
                    owner.session_id = "expiring-session-%d" % owner.session_seq
                    owner.initializes += 1
                    session_id = owner.session_id
                self._respond(message, {"Mcp-Session-Id": session_id})
                return
            session_id = self.headers.get("Mcp-Session-Id")
            if not session_id:
                self._send(400, compact({"error": "missing Mcp-Session-Id"}).encode("utf-8"))
                return
            with owner._lock:
                current = owner.session_id
                reject = owner.reject_sessions
            if reject or session_id != current:
                # A terminated (or never-accepted) session, exactly as the spec describes it.
                self._send(
                    404, compact({"error": "session %s is gone" % session_id}).encode("utf-8")
                )
                return
            if not self.headers.get("MCP-Protocol-Version"):
                self._send(
                    400, compact({"error": "missing MCP-Protocol-Version"}).encode("utf-8")
                )
                return
            self._respond(message, {})

        def _respond(self, message, extra):
            if message.get("id") is None:
                self._send(202, b"", extra)
                return
            if message.get("method") == "tools/call":
                with owner._lock:
                    owner.dispatched.append((message.get("params") or {}).get("name"))
            body = compact(fixture_handle(message)).encode("utf-8")
            self._send(200, body, extra)

        def _send(self, status, body, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if body:
                self.wfile.write(body)

    return Handler


class Workdir:
    """A scratch directory holding the generated config for one proxy run."""

    def __init__(self):
        self.path = tempfile.mkdtemp(prefix="lean-proxy-test-")

    def config(self, upstreams, **knobs):
        payload = dict(knobs)
        payload["upstreams"] = upstreams
        target = os.path.join(self.path, "config.json")
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return target

    def close(self):
        shutil.rmtree(self.path, ignore_errors=True)


def stdio_upstream(label="", command=None, args=None, pid_file=None, **extra):
    if args is None:
        args = [FIXTURE_STDIO]
        if label:
            args += ["--label", label]
        if pid_file:
            args += ["--pid-file", pid_file]
    spec = {"command": command or sys.executable, "args": args}
    spec.update(extra)
    return spec


def start_proxy(config_path, env=None):
    """Start the proxy as its own stdio MCP server and complete no handshake yet."""
    return Client(
        [sys.executable, os.path.join(SERVICES, "server.py"), "--config", config_path],
        cwd=SERVICES,
        env=env,
    )


def wait_for(predicate, timeout=5.0, interval=0.1):
    """Poll until `predicate` is true; returns the last value it produced."""
    deadline = time.time() + timeout
    value = None
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return value
