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

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.dirname(HERE)
SERVER = os.path.join(SERVICES, "server.py")
FIXTURE_STDIO = os.path.join(HERE, "fixture_stdio_mcp.py")
FIXTURE_HTTP = os.path.join(HERE, "fixture_http_mcp.py")
COMPACT = {"ensure_ascii": False, "separators": (",", ":")}


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
