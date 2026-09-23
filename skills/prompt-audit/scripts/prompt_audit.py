#!/usr/bin/env python3
"""Audit what occupies a Kimi Code session's prompt and context.

Reads agents/main/wire.jsonl of a session and reports:
  - system prompt sections (chars), split by top-level markdown heading
  - tool definition groups (chars of compact JSON), builtin vs each MCP server
  - per-request input/output tokens from usage.record events

Session data is local diagnostic material; the report contains only sizes.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

HEADING = re.compile(r"^# (.+)$", re.M)


def kimi_home() -> Path:
    env = os.environ.get("KIMI_CODE_HOME")
    return Path(env) if env else Path.home() / ".kimi-code"


def read_jsonl(path: Path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def compact(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def resolve_session_dir(home: Path, raw: str, quiet: bool = False):
    d = Path(raw)
    candidates = [d] if d.is_absolute() else [home / "sessions" / raw, home / raw]
    for c in candidates:
        if (c / "agents" / "main" / "wire.jsonl").exists():
            return c
    if quiet:
        return None
    sys.exit(f"error: session directory not found or has no main wire.jsonl: {raw}")


def entry_updated(home: Path, entry: dict) -> float:
    d = resolve_session_dir(home, entry.get("sessionDir", ""), quiet=True)
    if d is None:
        return 0
    state = d / "state.json"
    try:
        return json.loads(state.read_text(encoding="utf-8")).get("updatedAt") or 0
    except Exception:
        return 0


def find_session(args) -> Path:
    home = kimi_home()
    if args.session_dir:
        return resolve_session_dir(home, args.session_dir)
    index = home / "session_index.jsonl"
    if not index.exists():
        sys.exit(f"error: session index not found: {index}")
    entries = read_jsonl(index)
    if args.session:
        matches = [e for e in entries
                   if str(e.get("sessionId", "")) == args.session
                   or str(e.get("sessionId", "")).startswith(args.session)]
        if not matches:
            sys.exit(f"error: no session matching '{args.session}'")
    else:
        cwd = os.getcwd()
        matches = [e for e in entries if e.get("workDir") == cwd]
        if not matches:
            sys.exit(f"error: no session found for workDir {cwd} "
                     "(use --session or --session-dir)")
    best = max(matches, key=lambda e: entry_updated(home, e))
    return resolve_session_dir(home, best.get("sessionDir", ""))


def section_rows(prompt: str):
    heads = list(HEADING.finditer(prompt))
    rows = []
    if heads and heads[0].start() > 0:
        rows.append(("(preamble)", len(prompt[: heads[0].start()])))
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(prompt)
        rows.append((h.group(1).strip(), end - h.start()))
    if not rows:
        rows.append(("(whole prompt)", len(prompt)))
    return rows


def tool_group(name: str) -> str:
    m = re.match(r"mcp__(.+)__", name)
    return f"mcp:{m.group(1)}" if m else "builtin"


def audit(session_dir: Path) -> dict:
    wire = session_dir / "agents" / "main" / "wire.jsonl"
    rows = read_jsonl(wire)
    profile = next((r for r in rows if r.get("type") == "profile.bind"), {})
    snapshot = next((r for r in rows if r.get("type") == "llm.tools_snapshot"), {})
    first_req = next((r for r in rows if r.get("type") == "llm.request"), {})

    prompt = profile.get("systemPrompt") or ""
    tools = snapshot.get("tools") or []
    groups = {}
    for t in tools:
        g = tool_group(t.get("name", "?"))
        agg = groups.setdefault(g, {"count": 0, "chars": 0})
        agg["count"] += 1
        agg["chars"] += len(compact(t))

    requests = []
    for r in rows:
        if r.get("type") != "usage.record":
            continue
        u = r.get("usage") or {}
        requests.append({
            "input": (u.get("inputOther", 0) + u.get("inputCacheRead", 0)
                      + u.get("inputCacheCreation", 0)),
            "output": u.get("output", 0),
        })

    return {
        "dir": str(session_dir),
        "model": first_req.get("modelAlias") or first_req.get("model") or "",
        "prompt_chars": len(prompt),
        "sections": section_rows(prompt),
        "tool_count": len(tools),
        "tool_chars": len(compact(tools)),
        "groups": groups,
        "static_chars": len(prompt) + len(compact(tools)),
        "first_input_tokens": requests[0]["input"] if requests else None,
        "requests": requests,
    }


def render(a: dict) -> str:
    out = [f"## Prompt audit: `{a['dir']}`"]
    if a["model"]:
        out.append(f"Model: `{a['model']}`")
    tot = a["static_chars"] or 1
    out += ["",
            f"Resident total (system prompt + tool definitions): "
            f"**{a['static_chars']:,} chars**",
            "",
            "| Part | Chars | Share |",
            "|---|---:|---:|"]
    for name, chars in a["sections"]:
        out.append(f"| prompt: {name} | {chars:,} | {chars / tot:.1%} |")
    out.append(f"| **tool definitions ({a['tool_count']} tools)** "
               f"| **{a['tool_chars']:,}** | **{a['tool_chars'] / tot:.1%}** |")
    out += ["", "| Tool group | Tools | Chars |", "|---|---:|---:|"]
    for g, agg in sorted(a["groups"].items(), key=lambda kv: -kv[1]["chars"]):
        out.append(f"| {g} | {agg['count']} | {agg['chars']:,} |")
    if a["requests"]:
        out += ["", "| Request # | Input tokens | Output tokens |", "|---|---:|---:|"]
        for i, r in enumerate(a["requests"], 1):
            out.append(f"| {i} | {r['input']:,} | {r['output']:,} |")
    return "\n".join(out)


def render_compare(a: dict, b: dict) -> str:
    def fmt(v):
        return f"{v:,}" if isinstance(v, int) else "n/a"

    def delta(x, y):
        if isinstance(x, int) and isinstance(y, int):
            d = y - x
            return f"{d:+,}"
        return "n/a"

    return "\n".join([
        "## Compare",
        "",
        "| Metric | A | B | Delta (B - A) |",
        "|---|---:|---:|---:|",
        f"| Resident chars | {fmt(a['static_chars'])} | {fmt(b['static_chars'])} "
        f"| {delta(a['static_chars'], b['static_chars'])} |",
        f"| Tool count | {a['tool_count']} | {b['tool_count']} "
        f"| {delta(a['tool_count'], b['tool_count'])} |",
        f"| First-request input tokens | {fmt(a['first_input_tokens'])} "
        f"| {fmt(b['first_input_tokens'])} "
        f"| {delta(a['first_input_tokens'], b['first_input_tokens'])} |",
        "",
        f"A = `{a['dir']}`",
        f"B = `{b['dir']}`",
    ])


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", help="session id (or unique prefix)")
    ap.add_argument("--session-dir", help="path to a session directory")
    ap.add_argument("--compare", nargs=2, metavar=("DIR_A", "DIR_B"),
                    help="compare two session directories")
    args = ap.parse_args()

    if args.compare:
        home = kimi_home()
        a = audit(resolve_session_dir(home, args.compare[0]))
        b = audit(resolve_session_dir(home, args.compare[1]))
        print(render_compare(a, b))
        return

    print(render(audit(find_session(args))))


if __name__ == "__main__":
    main()
