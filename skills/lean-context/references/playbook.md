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
- A skill can chain: body → reference file → deeper file. Three levels is plenty.

## 3. Move tool schemas out of the main agent

Every tool exposed to an agent costs its `name + description + parameter schema` on every request. Two levers:

### 3.1 Delegate to specialist sub-agents

The main agent drops the tool (`disallowedTools`), and a custom sub-agent carries it. The schema then only occupies the sub-agent's own context, only when delegated to:

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

### 3.2 Defer MCP tools (experimental)

Kimi Code CLI can keep an MCP server's tools out of the top-level list and load them on demand via `select_tools`. Three prerequisites, all required:

1. `KIMI_CODE_EXPERIMENTAL_TOOL_SELECT=1` (or `[experimental] tool-select = true` in `config.toml`)
2. The model declares the `dynamically_loaded_tools` capability (official models do; others need `capabilities` in `config.toml`)
3. The server entry in `mcp.json` sets `"deferred": true`

```json
{
  "mcpServers": {
    "github": { "url": "https://mcp.example.com/mcp", "deferred": true }
  }
}
```

If any prerequisite is missing, `deferred` is silently ignored and tools load inline. Verify with `prompt-audit`: the server's tool group should disappear from the resident tool definitions.

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
