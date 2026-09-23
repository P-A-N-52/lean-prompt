#!/usr/bin/env python3
"""HTTP fixture: MCP streamable HTTP, either a JSON body or a one-message SSE stream.

Deliberately strict, so the proxy's client half is actually tested:
  * the session id handed out on initialize must be replayed afterwards;
  * `MCP-Protocol-Version` must be present on every request after initialize;
  * notifications are answered with 202 and an empty body.

Prints `PORT=<n>` on stdout so a test can start it on an ephemeral port.
"""

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fixture_core import compact, handle  # noqa: E402

SESSION_ID = "fixture-session-1"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "lpfixture-http/0.1.0"

    def log_message(self, fmt, *args):
        if self.server.verbose:
            sys.stderr.write("[fixture-http] %s\n" % (fmt % args))
            sys.stderr.flush()

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _send(self, status, payload=None, content_type="application/json", extra=None):
        body = b"" if payload is None else payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_POST(self):
        try:
            message = json.loads(self._body().decode("utf-8"))
        except ValueError:
            self._send(400, compact({"error": "body is not JSON"}))
            return
        method = message.get("method") if isinstance(message, dict) else None
        extra = {}
        if method == "initialize":
            extra["Mcp-Session-Id"] = SESSION_ID
        elif self.headers.get("Mcp-Session-Id") != SESSION_ID:
            self._send(400, compact({"error": "missing Mcp-Session-Id"}))
            return
        if method != "initialize" and not self.headers.get("MCP-Protocol-Version"):
            self._send(400, compact({"error": "missing MCP-Protocol-Version"}))
            return
        if message.get("id") is None:
            self._send(202, None, extra=extra)
            return
        response = handle(message, self.server.label)
        if response is None:
            self._send(202, None, extra=extra)
            return
        payload = compact(response)
        if self.server.mode == "sse":
            self._send(200, "event: message\ndata: %s\n\n" % payload,
                       content_type="text/event-stream", extra=extra)
        else:
            self._send(200, payload, extra=extra)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="fixture_http_mcp")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--mode", choices=("json", "sse"), default="sse")
    parser.add_argument("--label", default="")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.mode = args.mode
    server.label = "[%s]" % args.label if args.label else ""
    server.verbose = args.verbose
    sys.stdout.write("PORT=%d\n" % server.server_address[1])
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
