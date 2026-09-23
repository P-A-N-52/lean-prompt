---
name: prompt-audit
description: Measures what occupies a Kimi Code session's prompt — system prompt sections, tool definition groups, and per-request token usage — from the session's wire.jsonl
whenToUse: When the user asks how large the prompt or context is, what dominates it, or wants a before/after comparison of a prompt change
---

Run the audit script, then interpret the numbers for the user:

```sh
python3 ${KIMI_SKILL_DIR}/scripts/prompt_audit.py
```

- No arguments: audits the latest session of the current working directory (latest by `state.json` `updatedAt`, whichever format that file stores it in).
- `--session <id>` or `--session-dir <path>`: audit a specific session. A `--session` value must match one session — if the prefix matches several, the script lists the candidates and exits instead of guessing.
- `--compare <sessionDirA> <sessionDirB>`: before/after comparison of two sessions (e.g. without and with a prompt change).

Interpretation guidance:

- Resident parts (system prompt + tool definitions) are sent on every request; dynamic parts (messages, tool results) grow per turn.
- Tool definitions usually dominate the resident part; fetched documents and large tool results dominate growth.
- Section/group sizes are local character counts (compact JSON for tools); token rows are real tokens from `usage.record` events. Do not present char counts as exact tokens.
- The token table is labelled by **usage record**, not by request: a session logs many `llm.request` events but fewer `usage.record`s, and a record can carry another model's alias (delegated or differently-routed calls). The report prints both counts and the gap, so treat missing rows as missing data rather than as zero-token requests. `first usage-record input tokens` is the first request *that has* a usage record, not necessarily the session's first request — the same applies to the `--compare` row.
- Sections split on top-level `# ` headings only; a `#` line inside a fenced code block (an injected directory listing or `AGENTS.md`) is content, not a section boundary.
- If the wire has no `profile.bind` system prompt or no `llm.tools_snapshot`, that part is reported as *not recorded*, the resident total is withheld, and the report opens with an "Incomplete data" warning; if it has neither, or no readable events at all, the script exits with an error. Never quote a resident total from a session whose snapshot is missing.
- Unparsable lines — a session being written while you audit it, a torn write, invalid bytes — are skipped and counted. A non-zero count makes the figures a lower bound.
- If an expected reduction did not materialize, check whether the session predates the change — prompts are bound when the session's agent binds, so audit a session created after the change.
