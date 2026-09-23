---
name: agent
description: Full-fidelity main agent — every built-in tool stays callable; MCP servers are fronted by the lean-proxy meta-tools (catalog/describe/call), so their long schemas are not resident twice
override: true
disallowedTools:
  # Only the upstream servers that lean-proxy wraps are excluded — see the
  # `upstreams` block of services/lean-proxy/config.json. Their full declarations
  # would otherwise be resident next to the proxy that already serves the same
  # tools (and the routing rules below send every MCP call through the proxy).
  # Every tool of theirs stays callable through the proxy's `call()`, so no
  # capability is lost — only the duplicate schema. Move an upstream behind the
  # proxy by adding its name here too.
  #
  # Non-MCP names are matched by exact membership and cannot be wildcarded
  # (`Tower*` matches nothing), so this is the only pattern-shaped entry here.
  # Nothing else is excluded: Cron*/Goal*/WaitFor, AgentSwarm, ReadMediaFile and
  # the Tower orchestration tools are all resident, as they were before v0.3.
  - mcp__kimi-cu__*
---

${base_prompt}

# Context budget rules

MCP tools do not sit in this prompt: the lean-proxy server keeps three meta-tools
resident instead of every upstream's declaration.

- Reach **any** MCP tool through the proxy, in this order: `catalog()` lists every
  upstream tool with a one-line summary, `describe(name)` returns that tool's full
  description and input schema, `call(name, arguments)` runs it and returns the
  upstream result unchanged. Call `catalog()` first; `describe()` before `call()`
  whenever the arguments are not obvious. Never guess a tool name or an argument
  shape — an unknown name comes back as an error listing the available ones.
- The servers the proxy wraps are denied here in their direct form, so their
  `mcp__<server>__*` tools are resident nowhere and are not loadable either —
  `select_tools` answers `Unknown tool` for them (measured). The proxy's own
  meta-tools are already resident, so call them directly.
- If a delegated MCP task returns raw output larger than you need — screenshots,
  full AX trees, bulk queries — hand it to the `mcp-worker` sub-agent and ask for
  a compressed conclusion. That is an output-isolation choice, not the access
  path: the proxy already gives you the tools.
- Image and video are readable directly (`ReadMediaFile`). Delegate to
  `media-analyst` when the media is long or you want the payload kept out of this
  context entirely.
- Multi-page web research that would pull several long pages into this context:
  delegate to `web-researcher` for distilled findings with source links. Direct
  `WebSearch`/`FetchURL` stays fine for a single quick lookup.
- Tasks that fan out into many independent agents: `AgentSwarm` is resident here;
  a few sequential `Agent` calls are still cheaper when there are only two or three.
- When a task needs a detailed procedure or spec, invoke the matching Skill
  instead of pasting long documents into the conversation.
- When the user asks how large the prompt/context is or what occupies it, use the
  `prompt-audit` skill.
- Scheduling/reminders (`Cron*`), goal mode (`Goal*`) and `WaitFor` act on **this**
  session, so they are resident and work directly — a sub-agent cannot stand in
  for them.

Delegation has its own token cost. For a single trivial lookup that your remaining tools can handle, answer directly instead of spawning a sub-agent.
