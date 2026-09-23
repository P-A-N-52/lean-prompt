#!/usr/bin/env python3
"""stdio JSON-RPC MCP fixture: one JSON message per line, as lean-proxy speaks.

Run by the proxy itself (see tests/test_proxy.py) — never by hand except to poke
at the protocol. Serializes compactly and unescaped so that the raw text of a
`result` is comparable between a direct call and the same call through the proxy.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fixture_core import compact, handle, should_exit_after  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(prog="fixture_stdio_mcp")
    parser.add_argument("--label", default="", help="prefix folded into every result")
    parser.add_argument("--pid-file", default="", help="write this process's pid here on start")
    args = parser.parse_args(argv)

    if args.pid_file:
        with open(args.pid_file, "w", encoding="utf-8") as stream:
            stream.write(str(os.getpid()))

    label = "[%s]" % args.label if args.label else ""
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if isinstance(request, list):
            responses = [r for r in (handle(item, label) for item in request) if r]
            if responses:
                out.write(compact(responses) + "\n")
                out.flush()
            continue
        response = handle(request, label)
        if response is None:
            continue
        out.write(compact(response) + "\n")
        out.flush()
        if should_exit_after(request):
            out.flush()
            os._exit(0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
