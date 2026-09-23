---
name: lean-context
description: Progressive-disclosure playbook — keep the resident prompt small by moving tool schemas to sub-agents, knowledge to skills, and details to reference files
whenToUse: When designing or refactoring prompts, AGENTS.md files, skills, plugins, or agent tool sets for context efficiency
---

Apply three layers; each holds only what the current decision needs:

1. **Resident** — global constraints and routing rules only: base prompt, an AGENTS.md index, short skill descriptions.
2. **On-demand** — full procedures load only when the task matches: skill bodies, sub-agent contexts, deferred MCP tools.
3. **Reference** — long specs and examples stay in files that skills read only when needed.

Working rules:

- Audit before optimizing: run the `prompt-audit` skill to see what actually occupies the context.
- Tool definitions usually dominate the resident prompt. Prefer native deferral (`mcp.json` `"deferred": true` + `select_tools`) where the model supports it; otherwise exclude rarely used tools from the main agent (`disallowedTools`) and delegate to sub-agents that carry them. Session-state tools (`Cron*`, goal mode, `WaitFor`) cannot be delegated — for those, denying the tool *is* removing the feature, so make that trade explicit.
- AGENTS.md is an index, not a manual: link to docs, don't paste them.
- A plugin `systemPrompt` or `sessionStart.skill` is resident text on every session — keep it short or don't use it.
- Delegation costs tokens too; don't spawn a sub-agent for a trivial lookup.
- Context compaction shrinks content after it entered; progressive disclosure keeps it out in the first place. They are complements, not substitutes.

For the full methodology — AGENTS.md indexing patterns, MCP `deferred: true` setup, sub-agent design trade-offs, and which session-state tools can only be cut rather than delegated — read `references/playbook.md` in this skill's directory.
