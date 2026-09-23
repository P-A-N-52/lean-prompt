#!/usr/bin/env python3
"""The tool table both fixture transports serve.

The fixtures exist to answer one question about lean-proxy: does a result cross
the proxy unchanged? So the table covers every shape a real upstream can return —
plain text, an image content block with base64 payload, structuredContent,
non-ASCII text, a large payload, a tool-level error (`isError`) and a protocol
error — plus a tool that sleeps (timeouts) and one that exits (a dead session).

`label` is folded into every tool result so that two instances of the same
fixture are distinguishable, which is what the name-collision check needs: both
instances expose identical tool names.
"""

import json
import time

PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/"
    "q842iQAAAABJRU5ErkJggg=="
)

# Longer than any sensible catalog summary, with newlines and indentation that a
# one-line summary has to collapse.
LONG_DESCRIPTION = """Echo back the given text, prefixed with the fixture label.

This description is deliberately long: the proxy's catalog is supposed to
collapse it to a single line of about 120 characters while describe() still
returns every word of it. A fixture that shipped a tidy one-line description
could not tell those two behaviours apart."""


def build_tools(label=""):
    """The fixture's tools/list payload. `label` only affects results, never names."""
    return [
        {
            "name": "echo",
            "description": LONG_DESCRIPTION,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to echo."},
                    "repeat": {
                        "type": "integer",
                        "description": "How many times to repeat it.",
                        "default": 1,
                        "minimum": 1,
                    },
                },
                "required": ["text"],
            },
        },
        {
            "name": "picture",
            "title": "One pixel of PNG",
            "description": "Return a text block followed by an image block.",
            "annotations": {"readOnlyHint": True, "title": "picture"},
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "structured",
            "description": "Return text plus structuredContent with nested lists.",
            "outputSchema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "echoes": {"type": "array", "items": {"type": "string"}},
                    "nested": {"type": "object", "properties": {"depth": {"type": "integer"}}},
                },
                "required": ["ok"],
            },
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "options": {
                        "type": "object",
                        "properties": {
                            "limit": {"type": "integer", "default": 10},
                            "mode": {"type": "string", "enum": ["fast", "exact"]},
                            "tags": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["mode"],
                        "additionalProperties": False,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "name": "unicode",
            "description": "Return non-ASCII text.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "large",
            "description": "Return a large text payload.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "fail",
            "description": "Return a tool-level error (isError true).",
            "inputSchema": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
        {
            "name": "boom",
            "description": "Answer with a JSON-RPC error instead of a result.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "slow",
            "description": "Sleep before answering.",
            "inputSchema": {
                "type": "object",
                "properties": {"seconds": {"type": "number", "default": 3}},
            },
        },
        {
            "name": "exit_now",
            "description": "Answer, then exit the fixture process.",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


def text_block(text):
    return {"type": "text", "text": text}


def handle(request, label=""):
    """Answer one JSON-RPC message. Returns None for notifications."""
    method = request.get("method")
    rid = request.get("id")
    params = request.get("params") or {}
    if method == "initialize":
        return _ok(
            rid,
            {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "lpfixture", "version": "0.1.0"},
            },
        )
    if method == "tools/list":
        return _ok(rid, {"tools": build_tools(label)})
    if method == "tools/call":
        return _call(rid, params, label)
    if method in ("resources/list", "prompts/list", "resources/templates/list"):
        key = {
            "resources/list": "resources",
            "prompts/list": "prompts",
            "resources/templates/list": "resourceTemplates",
        }[method]
        return _ok(rid, {key: []})
    if method == "ping":
        return _ok(rid, {})
    if rid is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "error": {"code": -32601, "message": "Method not found: %s" % method},
    }


def _ok(rid, result):
    if rid is None:
        return None
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _call(rid, params, label):
    name = params.get("name")
    args = params.get("arguments") or {}
    if name == "echo":
        text = str(args.get("text", ""))
        repeat = int(args.get("repeat", 1) or 1)
        return _ok(rid, {"content": [text_block("%secho:%s" % (label, text * repeat))]})
    if name == "picture":
        return _ok(
            rid,
            {
                "content": [
                    text_block("%spicture: one red pixel" % label),
                    {"type": "image", "data": PNG_BASE64, "mimeType": "image/png"},
                ],
                "isError": False,
            },
        )
    if name == "structured":
        query = str(args.get("query", ""))
        return _ok(
            rid,
            {
                "content": [text_block("%sstructured:%s" % (label, query))],
                "structuredContent": {
                    "ok": True,
                    "echoes": [query, "%s%s" % (label, query)],
                    "nested": {"depth": 2, "note": "café 世界 ☕", "empty": None, "ratio": 0.5},
                },
                "isError": False,
            },
        )
    if name == "unicode":
        return _ok(
            rid,
            {"content": [text_block("%s…—«»café 世界 ☕ ταξίδι ünïcödé" % label)], "isError": False},
        )
    if name == "large":
        return _ok(rid, {"content": [text_block(("%s" % label) + "x" * 65536)]})
    if name == "fail":
        return _ok(
            rid,
            {
                "content": [text_block("%sfail:%s" % (label, args.get("reason", "")))],
                "isError": True,
            },
        )
    if name == "boom":
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "error": {
                "code": -32000,
                "message": "fixture exploded on purpose",
                "data": {"hint": "protocol-level failure"},
            },
        }
    if name == "slow":
        time.sleep(float(args.get("seconds", 3)))
        return _ok(rid, {"content": [text_block("%sslow:done" % label)]})
    if name == "exit_now":
        return _ok(rid, {"content": [text_block("%sexit_now:remember" % label)], "isError": False})
    return _ok(
        rid,
        {"content": [text_block("unknown tool %s" % name)], "isError": True},
    )


def should_exit_after(request):
    """True when the fixture should kill itself once this request is answered."""
    params = request.get("params") or {}
    return request.get("method") == "tools/call" and params.get("name") == "exit_now"


def compact(message):
    """The fixture's wire format matches the proxy's, so raw result text is comparable."""
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))
