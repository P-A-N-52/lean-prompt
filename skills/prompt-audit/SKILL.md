---
name: prompt-audit
description: Measures what occupies a Kimi Code session's prompt — system prompt sections, tool definition groups, and per-request token usage — from the session's wire.jsonl
whenToUse: When the user asks how large the prompt or context is, what dominates it, or wants a before/after comparison of a prompt change
---

Run the audit script, then interpret the numbers for the user:

```sh
python3 ${KIMI_SKILL_DIR}/scripts/prompt_audit.py
```

- No arguments: audits the latest session of the current working directory.
- `--session <id>` or `--session-dir <path>`: audit a specific session.
- `--compare <sessionDirA> <sessionDirB>`: before/after comparison of two sessions (e.g. without and with a prompt change).

Interpretation guidance:

- Resident parts (system prompt + tool definitions) are sent on every request; dynamic parts (messages, tool results) grow per turn.
- Tool definitions usually dominate the resident part; fetched documents and large tool results dominate growth.
- Section/group sizes are local character counts (compact JSON for tools); per-request input/output are real tokens from usage records. Do not present char counts as exact tokens.
- If an expected reduction did not materialize, check whether the session predates the change — prompts are bound when the session's agent binds, so audit a session created after the change.
