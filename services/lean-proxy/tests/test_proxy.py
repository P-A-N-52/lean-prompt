#!/usr/bin/env python3
"""End-to-end checks for lean-proxy, against local fixture upstreams only.

Three properties the proxy exists to provide, and how each is pinned down here:

1. **Only three tools, and no tool is lost.** tools/list must be exactly
   catalog/describe/call, catalog() must name every fixture tool, and the pass
   must cost far less resident text than the schemas it replaces.
2. **describe() is faithful.** The declaration that comes back is deep-compared
   against the upstream's own tools/list payload, with long descriptions intact
   (the catalog truncates the same description to one line).
3. **call() is faithful.** For each result shape the fixture can produce — text,
   image with base64, structuredContent, non-ASCII, 64 KiB payload, and a
   tool-level `isError` — the raw JSON text of the `result` is compared between a
   direct call and the same call through the proxy. Equal text means values, key
   order and escaping all survived; the envelope's `id` is the only difference.

Failure paths get the same treatment: an upstream JSON-RPC error becomes a
JSON-RPC error here, a timeout keeps its own code, an unreachable upstream is
reported by name, and a dead session is either transparently reconnected or
reported clearly when it cannot be.

Run: python3 services/lean-proxy/tests/test_proxy.py [--filter substring] [-v]
"""

import argparse
import base64
import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fixture_core import PNG_BASE64  # noqa: E402
from mcp_client import (  # noqa: E402
    Client,
    ClientError,
    FIXTURE_STDIO,
    HttpFixture,
    Workdir,
    compact,
    meta,
    raw_result_text,
    start_proxy,
    stdio_upstream,
    wait_for,
)

CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


class Harness:
    """One proxy process in front of the given upstreams, with a scratch config."""

    def __init__(self, upstreams, **knobs):
        self.workdir = Workdir()
        if callable(upstreams):
            upstreams = upstreams(self.workdir.path)
        self.upstreams = upstreams
        self.config_path = self.workdir.config(upstreams, **knobs)
        self.client = None
        self.info = None

    def __enter__(self):
        self.client = start_proxy(self.config_path)
        self.info = self.client.initialize()
        return self

    def __exit__(self, *exc_info):
        if self.client is not None:
            self.client.close()
        self.workdir.close()
        return False

    def catalog(self, server=None):
        arguments = {"server": server} if server else None
        result = meta(self.client, "catalog", arguments)
        return result["content"][0]["text"], result["structuredContent"]

    def tool_slot(self, name, arguments=None, timeout=60):
        """Raw response for one upstream tool, invoked through call()."""
        return self.client.call("call", {"name": name, "arguments": arguments or {}}, timeout)

    def tool_result(self, name, arguments=None, timeout=60):
        slot = self.tool_slot(name, arguments, timeout)
        if "result" not in slot["msg"]:
            raise ClientError("%s failed: %s" % (name, slot["msg"].get("error")))
        return slot["msg"]["result"]

    def tool_names(self):
        return [tool["name"] for tool in self.catalog()[1]["tools"]]

    def server_status(self, name):
        for entry in self.catalog()[1]["servers"]:
            if entry["name"] == name:
                return entry
        return None


def direct_fixture(label=""):
    """A second fixture process, for the reference side of a passthrough comparison."""
    command = [sys.executable, FIXTURE_STDIO] + (["--label", label] if label else [])
    client = Client(command)
    client.initialize()
    return client


def expect_error(client, name, arguments, code, note=""):
    """Call a meta-tool that must fail and return the JSON-RPC error."""
    params = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    error = client.error("tools/call", params)
    assert error["code"] == code, "expected error %s, got %s (%s)" % (
        code,
        error["code"],
        error.get("message"),
    )
    return error


# --------------------------------------------------------------------------- #
# 1. the surface the client sees
# --------------------------------------------------------------------------- #


@check
def test_only_three_meta_tools_and_resident_cost():
    direct = direct_fixture()
    try:
        upstream_cost = sum(len(compact(tool)) for tool in direct.tools())
        upstream_count = len(direct.tools())
    finally:
        direct.close()

    with Harness({"fixture": stdio_upstream()}) as harness:
        tools = harness.client.tools()
        names = [tool["name"] for tool in tools]
        assert names == ["catalog", "describe", "call"], names
        assert harness.info["serverInfo"]["name"] == "lean-proxy"
        assert harness.info["capabilities"] == {"tools": {"listChanged": False}}
        assert "instructions" in harness.info

        resident = len(compact(tools)) + len(harness.info["instructions"])
        print(
            "      resident %d chars (3 meta-tools + instructions) vs %d chars of upstream "
            "schemas (%d tools) — %.0f%% smaller"
            % (
                resident,
                upstream_cost,
                upstream_count,
                100.0 * (1 - resident / float(upstream_cost)),
            )
        )
        assert resident < 2000, "three meta-tools cost %d chars" % resident
        assert harness.tool_names() == [
            "boom",
            "echo",
            "exit_now",
            "fail",
            "large",
            "picture",
            "slow",
            "structured",
            "unicode",
        ], harness.tool_names()


@check
def test_catalog_summaries_are_truncated_to_one_line():
    with Harness({"fixture": stdio_upstream()}, description_limit=120) as harness:
        text, structured = harness.catalog()
        summaries = {tool["name"]: tool["summary"] for tool in structured["tools"]}
        names = sorted(summaries)
        assert names == [
            "boom",
            "echo",
            "exit_now",
            "fail",
            "large",
            "picture",
            "slow",
            "structured",
            "unicode",
        ], names
        for name, summary in summaries.items():
            assert len(summary) <= 120, "%s summary is %d chars" % (name, len(summary))
            assert "\n" not in summary, "%s summary still has a newline" % name
        echo = summaries["echo"]
        assert echo.endswith("…"), echo
        assert structured["tools"][names.index("echo")]["description_chars"] > 300
        assert "fixture [stdio] online" in text
        assert "describe(name)" in text
        assert "echo" in text

        limited = harness.catalog(server="fixture")[1]
        assert len(limited["servers"]) == 1
        assert limited["servers"][0]["name"] == "fixture"

        error = expect_error(harness.client, "catalog", {"server": "nope"}, -32602)
        assert "unknown upstream" in error["message"], error
        assert error["data"]["servers"] == ["fixture"]


# --------------------------------------------------------------------------- #
# 2. describe()
# --------------------------------------------------------------------------- #


@check
def test_describe_returns_the_upstream_declaration_verbatim():
    direct = direct_fixture()
    try:
        declared = {tool["name"]: tool for tool in direct.tools()}
    finally:
        direct.close()

    with Harness({"fixture": stdio_upstream()}) as harness:
        for name, declaration in declared.items():
            result = meta(harness.client, "describe", {"name": name})
            described = result["structuredContent"]
            assert described["name"] == name
            assert described["server"] == "fixture"
            assert described["upstream_status"] == "online"
            for key, value in declaration.items():
                assert described[key] == value, "%s.%s differs" % (name, key)
            assert json.dumps(described["inputSchema"], **{"ensure_ascii": False}) == json.dumps(
                declaration["inputSchema"], ensure_ascii=False
            ), "%s schema key order changed" % name
            assert json.loads(result["content"][0]["text"])["inputSchema"] == declaration[
                "inputSchema"
            ]

        # The long description survives whole, having been collapsed in the catalog.
        long_one = meta(harness.client, "describe", {"name": "echo"})["structuredContent"]
        assert long_one["description"] == declared["echo"]["description"]
        assert len(long_one["description"]) > 120
        assert "\n" in long_one["description"]

        # Extra declaration fields are carried, not dropped.
        picture = meta(harness.client, "describe", {"name": "picture"})["structuredContent"]
        assert picture["annotations"] == {"readOnlyHint": True, "title": "picture"}
        assert picture["title"] == "One pixel of PNG"
        structured = meta(harness.client, "describe", {"name": "structured"})[
            "structuredContent"
        ]
        assert structured["outputSchema"] == declared["structured"]["outputSchema"]

        error = expect_error(harness.client, "describe", {"name": "nosuch"}, -32602)
        assert "unknown tool" in error["message"]
        assert "echo" in error["message"]
        assert "echo" in error["data"]["available"]
        assert error["data"]["tool_count"] == 9


# --------------------------------------------------------------------------- #
# 3. call() passthrough
# --------------------------------------------------------------------------- #


@check
def test_call_results_cross_the_proxy_byte_for_byte():
    calls = [
        ("echo", {"text": "hello", "repeat": 3}),
        ("picture", {}),
        ("unicode", {}),
        ("structured", {"query": "q", "options": {"mode": "fast", "tags": ["a", "b"]}}),
        ("large", {}),
        ("fail", {"reason": "because"}),
    ]
    direct = direct_fixture()
    try:
        with Harness({"fixture": stdio_upstream()}) as harness:
            for name, arguments in calls:
                reference = direct.call(name, arguments)
                through = harness.tool_slot(name, arguments)
                assert "result" in reference["msg"], name
                assert "result" in through["msg"], "%s: %s" % (name, through["msg"])
                expected = raw_result_text(reference["raw"])
                actual = raw_result_text(through["raw"])
                assert expected == actual, "%s changed in transit:\n  direct: %s\n  proxy:  %s" % (
                    name,
                    expected[:400],
                    actual[:400],
                )

            text = harness.tool_result("echo", {"text": "hi"})
            assert text["content"] == [{"type": "text", "text": "echo:hi"}], text

            picture = harness.tool_result("picture")
            assert [block["type"] for block in picture["content"]] == ["text", "image"]
            image = picture["content"][1]
            assert image["data"] == PNG_BASE64, "base64 payload changed"
            assert image["mimeType"] == "image/png"
            assert base64.b64decode(image["data"]).startswith(b"\x89PNG\r\n\x1a\n")
            assert picture["isError"] is False

            structured = harness.tool_result("structured", {"query": "q"})
            assert structured["structuredContent"]["ok"] is True
            nested = structured["structuredContent"]["nested"]
            assert nested == {
                "depth": 2,
                "note": "café 世界 ☕",
                "empty": None,
                "ratio": 0.5,
            }, nested

            unicode_result = harness.tool_result("unicode")
            assert "世界" in unicode_result["content"][0]["text"]
            assert "caf" in unicode_result["content"][0]["text"]

            large = harness.tool_result("large")
            assert len(large["content"][0]["text"]) == 65536
            assert set(large["content"][0]["text"]) == {"x"}
    finally:
        direct.close()


@check
def test_tool_level_error_stays_a_result():
    direct = direct_fixture()
    try:
        with Harness({"fixture": stdio_upstream()}) as harness:
            reference = direct.call("fail", {"reason": "nope"})
            through = harness.tool_slot("fail", {"reason": "nope"})
            assert "result" in through["msg"], through["msg"]
            assert "error" not in through["msg"]
            assert through["msg"]["result"]["isError"] is True
            assert through["msg"]["result"]["content"] == [
                {"type": "text", "text": "fail:nope"}
            ]
            assert raw_result_text(reference["raw"]) == raw_result_text(through["raw"])
    finally:
        direct.close()


@check
def test_upstream_protocol_error_becomes_a_jsonrpc_error():
    with Harness({"fixture": stdio_upstream()}) as harness:
        error = expect_error(harness.client, "call", {"name": "boom", "arguments": {}}, -32603)
        assert "fixture exploded on purpose" in error["message"], error
        assert error["data"]["reason"] == "upstream_error"
        assert error["data"]["upstream_code"] == -32000
        assert error["data"]["upstream"] == "fixture"

        # A protocol error must not poison the session.
        assert harness.tool_result("echo", {"text": "after"})["content"][0]["text"] == "echo:after"


@check
def test_unknown_tool_and_bad_arguments_are_invalid_params():
    with Harness({"fixture": stdio_upstream()}) as harness:
        error = expect_error(harness.client, "call", {"name": "nope"}, -32602)
        assert "unknown tool" in error["message"]
        assert error["data"]["available"], error

        error = expect_error(harness.client, "call", {"name": "echo", "arguments": "junk"}, -32602)
        assert "arguments must be an object" in error["message"]

        error = expect_error(harness.client, "call", {"arguments": {}}, -32602)
        assert "tool name" in error["message"]

        error = expect_error(harness.client, "nope", {}, -32602)
        assert "catalog, describe and call" in error["message"]
        assert error["data"]["available"] == ["catalog", "describe", "call"]


@check
def test_timeout_has_its_own_code_and_the_proxy_survives():
    with Harness({"fixture": stdio_upstream(timeout_seconds=2)}) as harness:
        started = time.time()
        error = expect_error(
            harness.client, "call", {"name": "slow", "arguments": {"seconds": 8}}, -32001
        )
        elapsed = time.time() - started
        assert elapsed < 7, "timeout took %.1fs" % elapsed
        assert "did not answer" in error["message"], error
        assert error["data"]["reason"] == "timeout"
        assert error["data"]["tool"] == "slow"


# --------------------------------------------------------------------------- #
# 4. upstream lifecycle
# --------------------------------------------------------------------------- #


@check
def test_same_name_in_two_upstreams_gets_a_server_prefix():
    upstreams = {
        "alpha": stdio_upstream("alpha"),
        "beta": stdio_upstream("beta"),
    }
    with Harness(upstreams) as harness:
        names = harness.tool_names()
        assert "alpha__echo" in names and "beta__echo" in names, names
        assert "echo" not in names, names
        assert len(names) == 18, len(names)

        assert harness.tool_result("alpha__echo", {"text": "x"})["content"][0]["text"] == (
            "[alpha]echo:x"
        )
        assert harness.tool_result("beta__echo", {"text": "x"})["content"][0]["text"] == (
            "[beta]echo:x"
        )

        error = expect_error(harness.client, "call", {"name": "echo"}, -32602)
        assert "alpha__echo" in error["message"] and "beta__echo" in error["message"]

        text, _ = harness.catalog()
        assert "alpha [stdio] online" in text and "beta [stdio] online" in text

        limited = harness.catalog(server="beta")[1]["tools"]
        assert all(tool["server"] == "beta" for tool in limited)
        assert all(tool["name"].startswith("beta__") for tool in limited)


@check
def test_unstartable_upstream_is_marked_and_does_not_break_the_rest():
    upstreams = {
        "ghost": {"command": "/nonexistent/ghost-mcp", "args": []},
        "fixture": stdio_upstream(),
    }
    with Harness(upstreams) as harness:
        text, structured = harness.catalog()
        ghost = [entry for entry in structured["servers"] if entry["name"] == "ghost"][0]
        assert ghost["status"] == "offline", ghost
        assert "ghost-mcp" in ghost["detail"], ghost
        assert "ghost [stdio] OFFLINE" in text, text
        assert ghost["tool_count"] == 0

        assert harness.tool_result("echo", {"text": "ok"})["content"][0]["text"] == "echo:ok"
        error = expect_error(harness.client, "call", {"name": "ghost__echo"}, -32602)
        assert "unknown tool" in error["message"]


@check
def test_dead_upstream_is_reported_when_it_cannot_come_back():
    def upstreams(scratch):
        token = os.path.join(scratch, "spawned-once")
        script = (
            "if [ -f %s ]; then echo 'fixture refuses to restart' >&2; exit 7; fi; "
            "touch %s; exec %s %s" % (token, token, sys.executable, FIXTURE_STDIO)
        )
        return {"flaky": {"command": "/bin/sh", "args": ["-c", script]}}

    with Harness(upstreams) as harness:
        assert harness.tool_result("exit_now")["content"][0]["text"] == "exit_now:remember"

        status = wait_for(
            lambda: (harness.server_status("flaky") or {}).get("status") == "offline", timeout=5
        )
        assert status, harness.server_status("flaky")

        # The declaration outlives the session, so the tool is still describable...
        described = meta(harness.client, "describe", {"name": "echo"})["structuredContent"]
        assert described["upstream_status"] == "offline"
        assert described["inputSchema"]["required"] == ["text"]

        # ...and the call says exactly why it cannot run, instead of a bare failure.
        error = expect_error(harness.client, "call", {"name": "echo", "arguments": {"text": "x"}}, -32603)
        assert error["data"]["reason"] == "offline", error
        assert error["data"]["upstream"] == "flaky"
        assert "flaky" in error["message"] and "offline" in error["message"], error

        text, _ = harness.catalog()
        assert "flaky [stdio] OFFLINE" in text, text


@check
def test_dead_upstream_comes_back_on_a_later_call():
    with Harness({"fixture": stdio_upstream()}) as harness:
        harness.tool_result("exit_now")

        def call_after_the_drop():
            slot = harness.tool_slot("echo", {"text": "back"}, timeout=20)
            if "result" in slot["msg"]:
                return slot["msg"]["result"]
            # The first call may race the death: its request can land in a pipe whose
            # reader is gone, which is indistinguishable from a call that ran, so the
            # honest answer is an offline error rather than a silent second attempt.
            assert slot["msg"]["error"]["data"]["reason"] == "offline", slot["msg"]
            return None

        result = wait_for(call_after_the_drop, timeout=10)
        assert result is not None, "the upstream never came back"
        assert result["content"][0]["text"] == "echo:back"
        assert harness.server_status("fixture")["status"] == "online"


@check
def test_disabled_upstream_is_skipped_not_fatal():
    upstreams = {
        "off": {"command": "/nonexistent/ghost-mcp", "enabled": False},
        "fixture": stdio_upstream(),
    }
    with Harness(upstreams) as harness:
        names = [entry["name"] for entry in harness.catalog()[1]["servers"]]
        assert names == ["fixture"], names
        assert harness.tool_result("echo", {"text": "ok"})["content"][0]["text"] == "echo:ok"


@check
def test_shutdown_takes_the_upstreams_with_it():
    def upstreams(scratch):
        return {"fixture": stdio_upstream(pid_file=os.path.join(scratch, "fixture.pid"))}

    with Harness(upstreams) as harness:
        pid_file = os.path.join(harness.workdir.path, "fixture.pid")
        assert harness.tool_result("echo", {"text": "up"})["content"][0]["text"] == "echo:up"
        assert wait_for(lambda: os.path.exists(pid_file), timeout=5), "no pid file from the fixture"
        with open(pid_file, encoding="utf-8") as stream:
            fixture_pid = int(stream.read().strip())

        harness.client.proc.terminate()
        harness.client.proc.wait(timeout=10)
        assert wait_for(lambda: not alive(fixture_pid), timeout=5), (
            "the fixture is still running as an orphan"
        )


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------- #
# 5. HTTP upstreams and protocol hygiene
# --------------------------------------------------------------------------- #


def _http_passthrough(mode):
    fixture = HttpFixture(mode=mode)
    try:
        assert fixture.url.endswith("/mcp")
        direct = direct_fixture()
        try:
            with Harness({"http-fixture": {"url": fixture.url, "transport": "http"}}) as harness:
                names = harness.tool_names()
                assert "echo" in names and "picture" in names, names
                status = harness.server_status("http-fixture")
                assert status["transport"] == "http" and status["status"] == "online", status

                described = meta(harness.client, "describe", {"name": "structured"})[
                    "structuredContent"
                ]
                assert described["server"] == "http-fixture"
                assert described["inputSchema"]["required"] == ["query"]

                for name, arguments in [
                    ("echo", {"text": "over-http"}),
                    ("picture", {}),
                    ("structured", {"query": "q"}),
                    ("fail", {"reason": "r"}),
                ]:
                    reference = direct.call(name, arguments)
                    through = harness.tool_slot(name, arguments)
                    assert "result" in through["msg"], "%s: %s" % (name, through["msg"])
                    assert raw_result_text(reference["raw"]) == raw_result_text(through["raw"]), (
                        "%s changed in transit over %s" % (name, mode)
                    )

                # The fixture rejects any request without the session id and the
                # protocol-version header, so reaching this point already proves the
                # proxy sends both.
                assert harness.tool_result("fail", {"reason": "r"})["isError"] is True
        finally:
            direct.close()
    finally:
        fixture.close()


@check
def test_http_upstream_json_body():
    _http_passthrough("json")


@check
def test_http_upstream_sse_stream():
    _http_passthrough("sse")


@check
def test_http_upstream_going_away_is_marked_offline():
    fixture = HttpFixture(mode="json")
    with Harness({"http-fixture": {"url": fixture.url, "transport": "http"}}) as harness:
        assert harness.tool_result("echo", {"text": "up"})["content"][0]["text"] == "echo:up"
        fixture.close()

        error = expect_error(harness.client, "call", {"name": "echo", "arguments": {"text": "x"}}, -32603)
        assert error["data"]["reason"] == "offline", error
        assert harness.server_status("http-fixture")["status"] == "offline"

        # The declaration survives, so the tool is still describable while it is down.
        described = meta(harness.client, "describe", {"name": "echo"})["structuredContent"]
        assert described["upstream_status"] == "offline"
    fixture.close()


@check
def test_protocol_hygiene():
    with Harness({"fixture": stdio_upstream()}) as harness:
        assert harness.client.result("ping", {}) == {}
        assert harness.client.result("resources/list", {}) == {"resources": []}
        assert harness.client.result("prompts/list", {}) == {"prompts": []}
        error = harness.client.error("tools/nonsense", {})
        assert error["code"] == -32601, error

        # Garbage in, still alive out, and nothing but JSON-RPC on stdout.
        harness.client.send_raw("{not json")
        harness.client.send_raw(compact({"jsonrpc": "2.0", "id": 9999, "method": "ping"}))
        assert harness.client.result("ping", {}) == {}
        noise = [line for line in harness.client.stderr if "non-JSON on stdout" in line]
        assert not noise, noise


def main(argv=None):
    parser = argparse.ArgumentParser(description="lean-proxy end-to-end checks")
    parser.add_argument("--filter", default="", help="only run checks whose name contains this")
    parser.add_argument("-v", "--verbose", action="store_true", help="print tracebacks")
    args = parser.parse_args(argv)

    selected = [fn for fn in CHECKS if args.filter in fn.__name__]
    if not selected:
        print("no checks match %r" % args.filter)
        return 2

    failures = []
    started_all = time.time()
    for fn in selected:
        started = time.time()
        try:
            fn()
        except Exception as exc:  # a failing check must not stop the suite
            failures.append((fn.__name__, exc))
            print("FAIL  %-52s %5.1fs  %s: %s" % (fn.__name__, time.time() - started, type(exc).__name__, exc))
            if args.verbose:
                traceback.print_exc()
        else:
            print("ok    %-52s %5.1fs" % (fn.__name__, time.time() - started))

    print(
        "\n%d/%d checks passed in %.1fs"
        % (len(selected) - len(failures), len(selected), time.time() - started_all)
    )
    if failures:
        print("failed: %s" % ", ".join(name for name, _ in failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
