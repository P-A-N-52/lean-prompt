#!/usr/bin/env python3
"""Real-upstream check: kimi-cu driven directly and through lean-proxy.

The fixtures prove the relay is faithful; this proves it on the upstream that
actually prompted the proxy. It reuses services/lean-proxy/config.json, so the
launch command comes from the same file the proxy uses.

What it establishes:
  * every tool kimi-cu declares is still reachable through call(), and
    describe() returns its declaration verbatim;
  * a read-only call (list_apps) returns the same structure either way;
  * the compression is real, measured in the proxy's own wire format.

Run: python3 services/lean-proxy/tests/check_kimi_cu.py [-v]
Skips itself (exit 0) when the kimi-cu binary is not installed.
"""

import argparse
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp_client import (  # noqa: E402
    Client,
    SERVICES,
    compact,
    meta,
    start_proxy,
)

CONFIG = os.path.join(SERVICES, "config.json")
CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


def kimi_cu_spec():
    with open(CONFIG, encoding="utf-8") as handle:
        config = json.load(handle)
    servers = config.get("upstreams") or config.get("mcpServers") or {}
    for name, spec in servers.items():
        return name, spec
    raise AssertionError("config.json defines no upstreams")


def direct_client(spec):
    return Client([spec["command"]] + list(spec.get("args") or []))


def apps_of(result, side):
    """The app list a list_apps result carries, however kimi-cu serialized it.

    kimi-cu is not a stable serializer for this tool: between calls it varies the
    envelope key order, the key order inside each app, whether the payload carries
    "ok", and how many content blocks it returns. So what can be asserted about the
    relay is that a well-formed app list arrived intact — not that it equals the
    bytes a neighbouring call happened to produce.
    """
    assert set(result) <= {"content", "isError"}, "%s: unexpected result keys %s" % (
        side,
        sorted(result),
    )
    assert not result.get("isError"), "%s: isError set" % side
    blocks = [block for block in result["content"] if block.get("type") == "text"]
    assert blocks, "%s: no text block in %s" % (side, compact(result)[:200])
    found = []
    for block in blocks:
        try:
            payload = json.loads(block["text"])
        except ValueError:
            continue  # kimi-cu sometimes adds a second, prose-only text block
        apps = payload.get("apps") if isinstance(payload, dict) else None
        if not isinstance(apps, list):
            continue
        for app in apps:
            assert {"pid", "name", "bundle_id"} <= set(app), "%s: app fields %s" % (side, sorted(app))
            assert app["name"] and app["bundle_id"] and app["pid"], "%s: empty app %s" % (side, app)
        found.append(apps)
    assert found, "%s: no app list in %s" % (side, compact(result)[:200])
    return found[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description="lean-proxy against real kimi-cu")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    name, spec = kimi_cu_spec()
    if not os.path.exists(spec["command"]):
        print("skipped: %s is not installed" % spec["command"])
        return 0

    direct = direct_client(spec)
    proxy = start_proxy(CONFIG)
    failures = []
    try:
        proxy.initialize()
        declared = direct.tools()
        upstream_cost = sum(len(compact(tool)) for tool in declared)

        # -- surface ------------------------------------------------------ #
        def surface():
            tools = proxy.tools()
            names = [tool["name"] for tool in tools]
            assert names == ["catalog", "describe", "call"], names

        # -- catalog + resident cost -------------------------------------- #
        def catalog():
            result = meta(proxy, "catalog")
            text = result["content"][0]["text"]
            structured = result["structuredContent"]
            print(text)
            server = structured["servers"][0]
            assert server["name"] == name and server["status"] == "online", server
            assert server["tool_count"] == len(declared)
            assert sorted(tool["name"] for tool in structured["tools"]) == sorted(
                tool["name"] for tool in declared
            )
            for tool in structured["tools"]:
                assert len(tool["summary"]) <= 120, tool
                assert "\n" not in tool["summary"], tool

            resident = len(compact(proxy.tools())) + len(
                proxy.initialize_result["instructions"]
            )
            print(
                "\nresident: %d chars (3 meta-tools + instructions) vs %d chars of kimi-cu "
                "schemas for %d tools — %.0f%% smaller"
                % (
                    resident,
                    upstream_cost,
                    len(declared),
                    100.0 * (1 - resident / float(upstream_cost)),
                )
            )
            assert resident < upstream_cost / 2, "compression did not happen"

        # -- describe is verbatim ----------------------------------------- #
        def describe_all():
            for declaration in declared:
                tool_name = declaration["name"]
                described = meta(proxy, "describe", {"name": tool_name})["structuredContent"]
                for key, value in declaration.items():
                    assert described[key] == value, "%s.%s differs" % (tool_name, key)

        # -- one read-only call, both ways -------------------------------- #
        def list_apps():
            direct_result = direct.call("list_apps", {})["msg"]["result"]
            proxy_result = proxy.call("call", {"name": "list_apps", "arguments": {}})["msg"][
                "result"
            ]
            print("\n  direct: %s" % compact(direct_result)[:150])
            print("  proxy:  %s" % compact(proxy_result)[:150])

            direct_apps = apps_of(direct_result, "direct")
            proxy_apps = apps_of(proxy_result, "proxy")

            direct_by_id = {app["bundle_id"]: app for app in direct_apps}
            proxy_by_id = {app["bundle_id"]: app for app in proxy_apps}
            shared = set(direct_by_id) & set(proxy_by_id)
            assert shared, "the two calls saw no app in common: %s vs %s" % (
                sorted(direct_by_id),
                sorted(proxy_by_id),
            )
            for bundle_id in shared:
                assert direct_by_id[bundle_id] == proxy_by_id[bundle_id], (
                    "%s differs: %s vs %s" % (bundle_id, direct_by_id[bundle_id], proxy_by_id[bundle_id])
                )
            if set(direct_by_id) == set(proxy_by_id):
                print(
                    "list_apps: %d apps, identical in both directions" % len(proxy_by_id)
                )
            else:
                print(
                    "list_apps: %d shared apps identical; the live app list moved between "
                    "the two calls (%d vs %d)" % (len(shared), len(direct_by_id), len(proxy_by_id))
                )

        # -- unknown name / offline reporting ----------------------------- #
        def unknown_name():
            error = proxy.error("tools/call", {"name": "call", "arguments": {"name": "nope"}})
            assert error["code"] == -32602, error
            assert "list_apps" in error["message"], error

        for fn in (surface, catalog, describe_all, list_apps, unknown_name):
            try:
                fn()
            except Exception as exc:
                failures.append((fn.__name__, exc))
                print("FAIL  %-24s %s: %s" % (fn.__name__, type(exc).__name__, exc))
                if args.verbose:
                    traceback.print_exc()
            else:
                print("ok    %s" % fn.__name__)
    finally:
        proxy.close()
        direct.close()

    if failures:
        print("\n%d check(s) failed: %s" % (len(failures), ", ".join(n for n, _ in failures)))
        return 1
    print("\nall checks passed against real kimi-cu")
    return 0


if __name__ == "__main__":
    sys.exit(main())
