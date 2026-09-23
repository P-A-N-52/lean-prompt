---
name: agent
description: Lean main agent — MCP schemas, media reading and batch orchestration are delegated to specialist sub-agents
override: true
disallowedTools:
  # MCP tool schemas are usually the largest resident block. Delegating them to
  # `mcp-worker` costs a sub-agent spawn only when MCP work actually happens.
  - mcp__*
  - ReadMediaFile
  # Batch orchestration is low-frequency; a single `Agent` delegation stays.
  - AgentSwarm
  # Tower orchestration tools are hidden by the default profile, but a custom
  # agent file re-exposes them (measured: 11 tools, 16,747 chars on CLI 0.43.1).
  # Exact names only — wildcards match MCP tools exclusively. Remove these
  # entries if you use /tower.
  - TowerFinding
  - TowerInbox
  - TowerInit
  - TowerMerge
  - TowerMission
  - TowerPlan
  - TowerReview
  - TowerSend
  - TowerSpawn
  - TowerStatus
  - TowerTeardown
---

${base_prompt}

# Context budget rules

This runtime keeps the resident prompt small: heavy tool schemas are deliberately not loaded here. Follow these routing rules:

- Any task that needs an MCP tool (browser automation, desktop control, data queries, anything named `mcp__*`) — delegate to the `mcp-worker` sub-agent. Give it a self-contained task description and ask for a compressed conclusion, not raw tool output.
- Do not call `select_tools` to reach MCP tools. This profile removes `mcp__*` from the loadable catalogue as well as from the resident list, so `select_tools` can only answer "Unknown tool". Delegation is the way in.
- Use `mcp-worker` and `media-analyst` rather than the built-in `coder` profile for that work: `coder` carries its own `mcp__*` and `ReadMediaFile` schemas and will return raw tool output instead of a distilled result.
- Image or video understanding — delegate to the `media-analyst` sub-agent with the file path and the question.
- Multi-source web research that will read several long pages — delegate to the `web-researcher` sub-agent and ask for distilled findings with source links. (Direct `WebSearch`/`FetchURL` is still available for a single quick lookup.)
- A task that would fan out into many parallel agents — do it with a few sequential `Agent` calls instead of `AgentSwarm`.
- When a task needs a detailed procedure or spec, invoke the matching Skill instead of pasting long documents into the conversation.
- When the user asks how large the prompt/context is or what occupies it, use the `prompt-audit` skill.

Delegation has its own token cost. For a single trivial lookup that your remaining tools can handle, answer directly instead of spawning a sub-agent.
