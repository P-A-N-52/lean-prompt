---
name: agent
description: Lean main agent — MCP schemas, media reading and batch orchestration are delegated to specialist sub-agents; low-frequency session-state features (Cron*/Goal*/WaitFor) are cut
override: true
disallowedTools:
  # MCP tool schemas are usually the largest resident block. Delegating them to
  # `mcp-worker` costs a sub-agent spawn only when MCP work actually happens.
  - mcp__*
  - ReadMediaFile
  # Batch orchestration is low-frequency; a single `Agent` delegation stays.
  - AgentSwarm
  # Tower orchestration tools are hidden by the default profile, but a custom
  # agent file re-exposes them (measured: 11 tools, 16,601 chars under
  # prompt_audit.py's compact-JSON unit). Exact names only — wildcards match
  # MCP tools exclusively. Remove these entries if you use /tower.
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
  # --- Session-state features cut in v0.3. There is no delegation path for
  # --- them: they act on the calling session, so denying them removes the
  # --- feature outright. Each entry says what it did and how to add it back.
  # Creates a scheduled prompt (`/cron` style reminder). Add back: delete this line.
  - CronCreate
  # Cancels a scheduled prompt. Add back: delete this line.
  - CronDelete
  # Lists scheduled prompts. Add back: delete this line.
  - CronList
  # Defines a goal-mode objective (`/goal`). Add back: delete this line.
  - CreateGoal
  # Reads the current goal-mode objective. Add back: delete this line.
  - GetGoal
  # Sets the goal's token/time budget. Add back: delete this line.
  - SetGoalBudget
  # Updates goal status or progress. Add back: delete this line.
  - UpdateGoal
  # Waits for background tasks without ending the turn. Add back: delete this line.
  - WaitFor
  # --- Kept on purpose: plan mode and task/question control are used often
  # --- enough that cutting them costs more than their schemas save.
  # EnterPlanMode / ExitPlanMode / AskUserQuestion / TodoList / TaskList /
  # TaskOutput / TaskStop all stay loaded.
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
- Scheduling/reminders, goal mode and waiting on background tasks are deliberately **not loaded** here — they act on this session and cannot be delegated. If the user asks for one, say the feature is not available in this profile and point at the README's 档位 table instead of improvising a workaround.

Delegation has its own token cost. For a single trivial lookup that your remaining tools can handle, answer directly instead of spawning a sub-agent.
