#!/usr/bin/env python3
"""Audit what occupies a Kimi Code session's prompt and context.

Reads agents/main/wire.jsonl of a session and reports:
  - system prompt sections (chars), split by top-level markdown heading;
    headings inside fenced code blocks are ignored
  - tool definition groups (chars of compact JSON), builtin vs each MCP server
  - input/output tokens per `usage.record` event

A session whose wire records no prompt or tool snapshot is reported as
incomplete rather than audited with fabricated zeros, and torn/invalid lines
in a live wire.jsonl are skipped instead of aborting the run.

Session data is local diagnostic material; the report contains only sizes.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HEADING = re.compile(r"^# (.+)$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


class AuditError(Exception):
    """The session lacks the data a trustworthy report needs."""


def kimi_home() -> Path:
    env = os.environ.get("KIMI_CODE_HOME")
    return Path(env) if env else Path.home() / ".kimi-code"


def read_jsonl(path: Path):
    """Parse a JSONL file, tolerating torn writes and invalid UTF-8.

    A live session's wire.jsonl is appended to while this runs, and old
    session files can contain invalid byte sequences, so a bad line is
    counted and skipped rather than raising. Only JSON objects are returned —
    every caller indexes rows as events. Returns (rows, skipped).
    """
    rows = []
    skipped = 0
    try:
        handle = open(path, encoding="utf-8", errors="replace")
    except OSError as exc:
        sys.exit(f"error: cannot read {path}: {exc}")
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except (UnicodeDecodeError, ValueError):
                skipped += 1
                continue
            if isinstance(value, dict):
                rows.append(value)
            else:
                skipped += 1
    return rows, skipped


def compact(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def as_number(value) -> int:
    """Token counts as an int; anything non-numeric counts as 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def resolve_session_dir(home: Path, raw: str, quiet: bool = False):
    d = Path(raw)
    candidates = [d] if d.is_absolute() else [home / "sessions" / raw, home / raw]
    for c in candidates:
        if (c / "agents" / "main" / "wire.jsonl").exists():
            return c
    if quiet:
        return None
    sys.exit(f"error: session directory not found or has no main wire.jsonl: {raw}")


def parse_updated_at(value) -> float:
    """Normalize state.json updatedAt to epoch milliseconds.

    Older sessions store epoch ints, newer ones ISO-8601 strings; comparing
    the raw values raises TypeError, so both shapes collapse to one scale.
    Anything unparsable sorts oldest.
    """
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return 0.0
    text = value.strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        pass
    if text[-1] in "Zz":
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp() * 1000.0


def entry_updated(home: Path, entry: dict) -> float:
    d = resolve_session_dir(home, entry.get("sessionDir", ""), quiet=True)
    if d is None:
        return 0.0
    try:
        state = json.loads((d / "state.json").read_text(encoding="utf-8",
                                                        errors="replace"))
    except (OSError, ValueError):
        return 0.0
    if not isinstance(state, dict):
        return 0.0
    return parse_updated_at(state.get("updatedAt"))


def describe_matches(home: Path, matches: list) -> str:
    ranked = sorted(matches, key=lambda e: entry_updated(home, e), reverse=True)
    lines = []
    for e in ranked[:20]:
        stamp = entry_updated(home, e)
        when = "unknown mtime" if not stamp else datetime.fromtimestamp(
            stamp / 1000.0, tz=timezone.utc).isoformat(timespec="seconds")
        lines.append(f"  {e.get('sessionId', '?')}  ({when}, {e.get('workDir', '?')})")
        lines.append(f"    {e.get('sessionDir', '?')}")
    if len(ranked) > 20:
        lines.append(f"  … and {len(ranked) - 20} more")
    return "\n".join(lines)


def find_session(args) -> Path:
    home = kimi_home()
    if args.session_dir:
        return resolve_session_dir(home, args.session_dir)
    index = home / "session_index.jsonl"
    if not index.exists():
        sys.exit(f"error: session index not found: {index}")
    entries, _ = read_jsonl(index)
    if args.session:
        matches = [e for e in entries
                   if str(e.get("sessionId", "")) == args.session
                   or str(e.get("sessionId", "")).startswith(args.session)]
        if not matches:
            sys.exit(f"error: no session matching '{args.session}'")
        exact = [e for e in matches if str(e.get("sessionId", "")) == args.session]
        if len(exact) == 1:
            matches = exact
        elif len(matches) > 1:
            sys.exit(f"error: session id '{args.session}' is ambiguous — "
                     f"{len(matches)} sessions match; use a longer prefix or "
                     f"--session-dir:\n{describe_matches(home, matches)}")
    else:
        cwd = os.getcwd()
        matches = [e for e in entries if e.get("workDir") == cwd]
        if not matches:
            sys.exit(f"error: no session found for workDir {cwd} "
                     "(use --session or --session-dir)")
    best = max(matches, key=lambda e: entry_updated(home, e))
    return resolve_session_dir(home, best.get("sessionDir", ""))


def heading_spans(prompt: str):
    """Top-level heading spans, skipping lines inside fenced code blocks.

    Prompts embed files (AGENTS.md, directory listings) in fenced blocks, and
    a `#` heading inside such a block is content, not a section boundary.
    """
    spans = []
    fence = None
    pos = 0
    for line in prompt.splitlines(keepends=True):
        m = FENCE.match(line)
        if fence is None:
            if m:
                fence = (m.group(1)[0], len(m.group(1)))
            else:
                h = HEADING.match(line.rstrip("\r\n"))
                if h:
                    spans.append((pos, h.group(1).strip()))
        elif (m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1]
              and line.strip() == m.group(1)):
            fence = None
        pos += len(line)
    return spans


def section_rows(prompt: str):
    if not prompt:
        return []
    heads = heading_spans(prompt)
    rows = []
    if heads and heads[0][0] > 0:
        rows.append(("(preamble)", heads[0][0]))
    for i, (start, title) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(prompt)
        rows.append((title, end - start))
    if not rows:
        rows.append(("(whole prompt)", len(prompt)))
    return rows


def tool_group(name: str) -> str:
    m = re.match(r"mcp__(.+)__", name)
    return f"mcp:{m.group(1)}" if m else "builtin"


def audit(session_dir: Path) -> dict:
    wire = session_dir / "agents" / "main" / "wire.jsonl"
    rows, skipped = read_jsonl(wire)
    if not rows:
        raise AuditError(
            f"{wire} holds no readable events — an empty or torn wire.jsonl "
            "cannot be audited")
    profile = next((r for r in rows if r.get("type") == "profile.bind"), {})
    snapshot = next((r for r in rows if r.get("type") == "llm.tools_snapshot"), {})
    llm_requests = [r for r in rows if r.get("type") == "llm.request"]
    first_req = llm_requests[0] if llm_requests else {}

    prompt = profile.get("systemPrompt") if isinstance(profile, dict) else None
    has_prompt = isinstance(prompt, str) and bool(prompt)
    prompt = prompt if has_prompt else ""
    raw_tools = snapshot.get("tools") if isinstance(snapshot, dict) else None
    has_tools = isinstance(raw_tools, list)
    tools = [t for t in raw_tools if isinstance(t, dict)] if has_tools else []

    if not has_prompt and not has_tools:
        raise AuditError(
            f"{wire} records neither a `profile.bind` system prompt nor an "
            "`llm.tools_snapshot` — no prompt snapshot to audit (a resident "
            "total computed from this would be fabricated)")

    groups = {}
    for t in tools:
        g = tool_group(t.get("name", "?"))
        agg = groups.setdefault(g, {"count": 0, "chars": 0})
        agg["count"] += 1
        agg["chars"] += len(compact(t))
    tool_chars = len(compact(tools)) if tools else 0

    records = []
    for r in rows:
        if r.get("type") != "usage.record":
            continue
        u = r.get("usage") if isinstance(r.get("usage"), dict) else {}
        records.append({
            "model": r.get("modelAlias") or r.get("model") or "",
            "input": (as_number(u.get("inputOther")) + as_number(u.get("inputCacheRead"))
                      + as_number(u.get("inputCacheCreation"))),
            "output": as_number(u.get("output")),
        })

    return {
        "dir": str(session_dir),
        "model": first_req.get("modelAlias") or first_req.get("model") or "",
        "has_prompt": has_prompt,
        "has_tools": has_tools,
        "skipped_lines": skipped,
        "prompt_chars": len(prompt),
        "sections": section_rows(prompt) if has_prompt else [],
        "tool_count": len(tools) if has_tools else None,
        "tool_chars": tool_chars,
        "groups": groups,
        "known_chars": len(prompt) + tool_chars,
        "static_chars": (len(prompt) + tool_chars
                         if has_prompt and has_tools else None),
        "llm_request_count": len(llm_requests),
        "usage_record_count": len(records),
        "first_input_tokens": records[0]["input"] if records else None,
        "records": records,
    }


def notes(a: dict) -> list:
    """Caveats that make the resident numbers unreliable (token gaps are
    reported next to the token table instead)."""
    out = []
    if not a["has_prompt"]:
        out.append("no `profile.bind` system prompt in this wire — system "
                   "prompt size is **not recorded**")
    if not a["has_tools"]:
        out.append("no `llm.tools_snapshot` in this wire — tool definitions "
                   "are **not recorded**")
    if a["skipped_lines"]:
        out.append(f"{a['skipped_lines']:,} line(s) of wire.jsonl were not "
                   "usable events (torn write, invalid bytes, or non-event "
                   "content) and were skipped")
    return out


def render(a: dict) -> str:
    out = [f"## Prompt audit: `{a['dir']}`"]
    if a["model"]:
        out.append(f"Model: `{a['model']}`")
    warn = notes(a)
    if warn:
        out += ["", "> **Incomplete data — read before quoting any number:**"]
        out += [f"> - {w}" for w in warn]

    tot = a["static_chars"] or a["known_chars"] or 1
    out += [""]
    if a["static_chars"] is not None:
        out.append("Resident total (system prompt + tool definitions): "
                   f"**{a['static_chars']:,} chars**")
    else:
        out.append("Resident total: **not available** — the parts marked "
                   "*not recorded* above are missing.")
    out += ["", "| Part | Chars | Share |", "|---|---:|---:|"]
    for name, chars in a["sections"]:
        out.append(f"| prompt: {name} | {chars:,} | {chars / tot:.1%} |")
    if not a["has_prompt"]:
        out.append("| prompt: *not recorded* | n/a | n/a |")
    if a["has_tools"]:
        out.append(f"| **tool definitions ({a['tool_count']} tools)** "
                   f"| **{a['tool_chars']:,}** | **{a['tool_chars'] / tot:.1%}** |")
    else:
        out.append("| **tool definitions: *not recorded*** | n/a | n/a |")

    if a["has_tools"]:
        out += ["", "| Tool group | Tools | Chars |", "|---|---:|---:|"]
        for g, agg in sorted(a["groups"].items(), key=lambda kv: -kv[1]["chars"]):
            out.append(f"| {g} | {agg['count']} | {agg['chars']:,} |")
    else:
        out += ["", "Tool group breakdown unavailable (no tools snapshot)."]

    if a["records"]:
        out += ["", "| Usage record # | Model | Input tokens | Output tokens |",
                "|---|---|---:|---:|"]
        for i, r in enumerate(a["records"], 1):
            out.append(f"| {i} | {r['model'] or '?'} | {r['input']:,} "
                       f"| {r['output']:,} |")
        gap = a["llm_request_count"] - a["usage_record_count"]
        token_note = (f"Rows are `usage.record` events, not requests — this "
                      f"session logged {a['llm_request_count']:,} `llm.request` / "
                      f"{a['usage_record_count']:,} `usage.record`")
        if gap > 0:
            token_note += f" ({gap:,} request(s) have no token row)"
        elif gap < 0:
            token_note += f" (records exceed requests by {-gap:,})"
        token_note += ("; some requests are missing here and a record can belong "
                       "to another model or a delegated call.")
        out += ["", token_note,
                f"First usage-record input tokens: "
                f"**{a['first_input_tokens']:,}** — the first request that has "
                "a usage record, which is not necessarily the session's first "
                "request."]
    else:
        out += ["", "No `usage.record` events — no per-request token data."]
    return "\n".join(out)


def render_compare(a: dict, b: dict) -> str:
    def fmt(v):
        return f"{v:,}" if isinstance(v, int) else "n/a"

    def delta(x, y):
        if isinstance(x, int) and isinstance(y, int):
            d = y - x
            return f"{d:+,}"
        return "n/a"

    out = ["## Compare",
           "",
           "| Metric | A | B | Delta (B - A) |",
           "|---|---:|---:|---:|",
           f"| Resident chars | {fmt(a['static_chars'])} | "
           f"{fmt(b['static_chars'])} | "
           f"{delta(a['static_chars'], b['static_chars'])} |",
           f"| Tool count | {fmt(a['tool_count'])} | {fmt(b['tool_count'])} "
           f"| {delta(a['tool_count'], b['tool_count'])} |",
           f"| First usage-record input tokens | {fmt(a['first_input_tokens'])} "
           f"| {fmt(b['first_input_tokens'])} "
           f"| {delta(a['first_input_tokens'], b['first_input_tokens'])} |",
           "",
           "`n/a` = that session's wire does not record the metric.",
           "Token rows are `usage.record` events; `first usage-record input "
           "tokens` is the first request with a usage record, not necessarily "
           "the session's first request.",
           ""]
    for label, x in (("A", a), ("B", b)):
        out.append(f"{label} = `{x['dir']}` ({x['llm_request_count']:,} "
                   f"llm.request / {x['usage_record_count']:,} usage.record)")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", help="session id (or unique prefix)")
    ap.add_argument("--session-dir", help="path to a session directory")
    ap.add_argument("--compare", nargs=2, metavar=("DIR_A", "DIR_B"),
                    help="compare two session directories")
    args = ap.parse_args()

    try:
        if args.compare:
            home = kimi_home()
            a = audit(resolve_session_dir(home, args.compare[0]))
            b = audit(resolve_session_dir(home, args.compare[1]))
            print(render_compare(a, b))
            return
        print(render(audit(find_session(args))))
    except AuditError as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":
    main()
