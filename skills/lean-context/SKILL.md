---
name: lean-context
description: Progressive-disclosure playbook — keep the resident prompt small by moving tool schemas behind a meta-tool proxy or into sub-agents, knowledge to skills, and details to reference files
whenToUse: When designing or refactoring prompts, AGENTS.md files, skills, plugins, or agent tool sets for context efficiency
---

Apply three layers; each holds only what the current decision needs:

1. **Resident** — global constraints and routing rules only: base prompt, an AGENTS.md index, short skill descriptions.
2. **On-demand** — full procedures load only when the task matches: skill bodies, sub-agent contexts, deferred or proxied MCP tools.
3. **Reference** — long specs and examples stay in files that skills read only when needed.

Working rules:

- Audit before optimizing: run the `prompt-audit` skill to see what actually occupies the context.
- Tool definitions usually dominate the resident prompt. Four dispositions, in order: **defer** (native MCP `"deferred": true` + `select_tools`, needs a flag and a capable model), **proxy** (front the servers with a meta-tool server — `catalog`/`describe`/`call` — which needs nothing from the host), **delegate** (exclude from the main agent, carry the tool in a sub-agent), **cut** (`disallowedTools`). The last one is only for session-state tools (`Cron*`, goal mode, `WaitFor`): no sub-agent can produce their outcome, so denying the tool *is* removing the feature — make that trade explicit.
- Moving something "into a file" only works when the file is the deliverable (knowledge) or the thing to run (a script). A capability the calling session must execute — a scheduler, goal mode, plan mode — cannot be loaded from a file.
- AGENTS.md is an index, not a manual: link to docs, don't paste them.
- A plugin `systemPrompt` or `sessionStart.skill` is resident text on every session — keep it short or don't use it.
- Delegation costs tokens too; don't spawn a sub-agent for a trivial lookup.
- Context compaction shrinks content after it entered; progressive disclosure keeps it out in the first place. They are complements, not substitutes.

For the full methodology — AGENTS.md indexing patterns, MCP `deferred: true` setup, the meta-tool proxy pattern and how it compares with Claude Code / Codex / OpenCode, sub-agent design trade-offs, and which session-state tools can only be cut rather than delegated — read `references/playbook.md` in this skill's directory.
