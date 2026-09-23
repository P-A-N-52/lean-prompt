# Progressive Disclosure Playbook

Reference material for the `lean-context` skill. Read the sections relevant to the task at hand.

## 1. Measure first

Run the `prompt-audit` skill (or `/lean-prompt:audit`) before and after any change. It reports, from the session's `wire.jsonl`:

- system prompt sections (chars), split by top-level heading
- tool definition groups (chars of compact JSON), split into builtin vs each MCP server
- per-request input/output tokens from usage records

Two rules of thumb from real measurements (Kimi Code CLI 0.43.1, a session with two MCP plugins enabled):

- Tool definitions were ~92% of the resident `system prompt + tools` payload; the agent instructions themselves were under 8%.
- Dynamic growth came mostly from tool results: six fetched documentation pages added ~93k chars, while the skill that triggered them was ~3k chars.

Optimizing instructions before auditing tools is usually wasted effort.

## 2. Layer your knowledge

| Layer | What lives here | Loaded when |
|---|---|---|
| Resident | Safety rules, global conventions, routing rules, a short index of everything else | Every request |
| Skill body | One focused procedure, kept short | The task matches its description |
| Reference file | Long specs, checklists, examples | The skill body points to it and the task needs it |

Concretely:

- Root `AGENTS.md` should read like a table of contents: one line per rule with a pointer (`docs/style.md`, `skills/…`). Content placed here is injected into every session's prompt — it is not retrieved per task.
- Keep `SKILL.md` bodies short; move detail into sibling files and reference them with `${KIMI_SKILL_DIR}`.
- Plugin `systemPrompt` / `systemPromptPath` is resident text (32 KB per field, 64 KB per prompt build across plugins). `sessionStart.skill` loads a skill into every session — both are anti-patterns for anything long.
- Reference files stay **one level deep** from `SKILL.md`. Anthropic's Agent Skills best practices ask for exactly that: a model that meets a nested reference may preview it with `head -100` instead of reading it whole, and comes back with incomplete information. If a reference needs more detail, deepen the file, not the chain.

## 3. Move tool schemas out of the main agent

Every tool exposed to an agent costs its `name + description + parameter schema` on every request. Two levers, in this order:

### 3.1 Defer MCP tools (native, preferred)

Kimi Code CLI keeps an MCP server's tool schemas out of the top-level `tools[]` and loads them on demand through `select_tools`. This is the cheap path: no sub-agent spawn, no second context, and the tool is callable in the same turn it is selected.

Two prerequisites, both required:

1. A runtime switch. Any one of these turns it on:
   - the master switch `KIMI_CODE_EXPERIMENTAL_FLAG=1` (enables every experimental flag, `tool-select` included);
   - the per-flag switch `KIMI_CODE_EXPERIMENTAL_TOOL_SELECT=1`;
   - `[experimental] tool-select = true` in `config.toml`.
2. The model declares the `dynamically_loaded_tools` capability alongside `tool_use` (the official models do; any other model needs a `[models."…"]` entry in `config.toml` that declares it).

`"deferred": true` is then the per-server **enabling field**, not a third prerequisite:

```json
{
  "mcpServers": {
    "github": { "url": "https://mcp.example.com/mcp", "deferred": true }
  }
}
```

Missing either prerequisite leaves the server inline and silent; missing the field leaves it inline. Verify with `prompt-audit`: the server's tool group must disappear from the resident tool definitions, and a `<tools_added>` system reminder must list its tool names on the first turn.

**Do not combine deferral with a `disallowedTools: [mcp__*]` denylist.** Measured on CLI 0.43.1: a denied MCP tool is filtered out of the *loadable* catalogue as well, so it never appears in `<tools_added>` and `select_tools` answers `Unknown tool: …`. The capability becomes unreachable rather than merely delegated. Pick one path per server — native deferral with no denylist entry for that server, or inline + denylist + delegation.

### 3.2 Delegate to specialist sub-agents (fallback)

The main agent drops the tool (`disallowedTools`) and a custom sub-agent carries it. Use this when deferral is unavailable (a model without `dynamically_loaded_tools`, a server that must stay inline) or when the sub-agent's distillation matters as much as the schema saving. The schema then only occupies the sub-agent's own context:

```yaml
# agents/agent.md (main, override)
disallowedTools:
  - mcp__*
  - ReadMediaFile
```

```yaml
# agents/mcp-worker.md (sub-agent)
tools:
  - mcp__*
  - Read
```

Trade-offs:

- Every use of the excluded capability now costs a sub-agent spawn (its own prompt + tokens + latency). Do this for tools that are heavy and occasional, not for tools used every turn.
- Sub-agent results come back as text; media and huge outputs should be distilled by the sub-agent before returning.
- Write the sub-agent's `description` as routing advice — it is what the main agent reads when deciding to delegate.
- A sub-agent's `tools` list is not a hard boundary for `select_tools`, but it is for everything else. The loader is gated by `disallowedTools`, not by the `tools` allow-list, so whenever tool-select is active for that agent's own model — the model declares `dynamically_loaded_tools` and `tool_use`, and the flag is on — the runtime adds `select_tools` on top of the declared tools. Measured on `kimi-code/k3`: a sub-agent declaring `[mcp__*, Read, Grep, Glob]` resolved to `[Glob, Grep, Read, select_tools]`. An agent on a model without that capability gets exactly its declared list and no loader — measured on `deepseek/deepseek-flash`, where `web-researcher` resolved to `[FetchURL, Read, WebSearch]` and `mcp-worker` to its declared MCP tools plus `Read/Grep/Glob`. Do not read the list as an exhaustive allow-list for the loader; do rely on it for the rest.
- `disallowedTools` matches non-MCP names by exact membership and MCP names (`mcp__*`) by glob — `Tower*` would match nothing.

### 3.3 Moving tools out also helps caching

Prompt caching keys on a stable request prefix, and the tool array sits at the front of it. A tool list that shifts mid-session — an MCP server reconnecting, a schema appearing when it was absent before — invalidates everything cached behind it. Curating the resident tools (deferring MCP schemas, denying the low-frequency heavy ones) keeps the prefix stable and cache hits predictable, which is worth more than the one-off character saving. Native deferral is built for this: deferred definitions stay out of the cacheable prefix rather than being reordered inside it.

## 4. Decide what stays resident

Keep resident only what must hold **even when the model forgets everything else**: safety boundaries, output contract, routing rules. Everything explanatory ("how to do X") is a candidate for a skill.

A quick test: if a paragraph only matters for one kind of task, it does not belong in the resident prompt.

## 5. Anti-patterns

- Pasting a whole style guide into `AGENTS.md` "so the model sees it".
- Plugin `systemPromptPath` pointing at a long manual.
- Connecting many MCP servers inline "just in case".
- Sub-agent per trivial step — delegation overhead exceeds the saved context.
- Relying on `/compact` as the design: it evicts content after it already cost tokens and attention.

## 6. Verifying a change

1. `prompt-audit` on a session before the change — note resident chars and first-request input tokens.
2. Start a **new** session after the change (prompts are bound at bind time; resumed sessions may keep the old profile).
3. `prompt-audit --compare <oldSessionDir> <newSessionDir>`.
4. Also verify behavior, not just size: can the agent still reach the excluded capability through the intended path (delegation or `select_tools`)?
