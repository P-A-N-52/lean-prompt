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

Every tool exposed to an agent costs its `name + description + parameter schema` on every request. Four dispositions, in this order — the first three move the work, the fourth removes it:

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

**Do not combine deferral with a `disallowedTools: [mcp__*]` denylist.** Measured on CLI 0.43.1: a denied MCP tool is filtered out of the *loadable* catalogue as well, so it never appears in `<tools_added>` and `select_tools` answers `Unknown tool: mcp__kimi-cu__list_apps. Pick from the latest announced tools list.` The capability becomes unreachable rather than merely delegated. Pick one path per server — native deferral with no denylist entry for that server, inline + denylist + delegation, or the proxy in 3.2.

The same reply comes back for a tool that is *already resident* (`select_tools: mcp__lean__catalog` → `Unknown tool`): the loader only serves deferred definitions, so a loaded tool is not reachable through it and does not need to be.

### 3.2 Front the servers with a meta-tool proxy (lean-proxy)

Native deferral needs three things to line up (flag, model capability, per-server field) and fails silently when one is missing. The third disposition needs nothing from the host: put a **meta-tool server** in front of the real MCP servers, and let it re-publish them as three compact tools.

`services/lean-proxy/` is this plugin's implementation of the pattern:

| meta-tool | what it does |
|---|---|
| `catalog()` | every upstream tool name + owning server + a one-line summary (truncated to `description_limit`, 120 chars by default) |
| `describe(name)` | that tool's full description and input schema, exactly as the upstream declared it |
| `call(name, arguments)` | forwards the call and returns the upstream result unchanged |

Nothing is removed: `call()` reaches every tool the upstreams expose, so this works for servers a sub-agent cannot drive (session state, side effects), and the model just looks the tool up first. Measured on CLI 0.43.1 with kimi-cu (10 tools): resident tool definitions for that server drop from 8,151 chars to the three meta-tools' 1,131 chars (−86%); a **second** upstream adds **0** resident chars, because its declarations only ever appear inside a `catalog()` result. Costs: one extra process per upstream, and one or two extra round trips per new tool (`catalog` then `describe`) — which is why the proxy's own tool descriptions say "start here, then describe, then call".

Four designs in the field, one shape:

| | mechanism | resident | fetch step | prerequisite |
|---|---|---|---|---|
| **Anthropic Tool Search** | `defer_loading: true` on tool definitions | 3–5 high-frequency tools | server-side search returns ~5 matches | API support (documented: 77K→8.7K tokens on one example) |
| **Claude Code** | MCP tool search, on by default (`ENABLE_TOOL_SEARCH`) | tool names + server instructions | search, `auto` threshold at 10% of context | client feature |
| **Codex** | `tool_search` (BM25 over deferred metadata, default limit 8) | `ToolExposure::Direct` tools only | search by query | model advertises `supports_search_tool`, namespaces enabled |
| **OpenCode** (experimental code mode) | MCP folded into one catalog + one `execute` tool | the catalog + `execute` | model writes code against the catalog | client feature flag |
| **lean-proxy** | a normal MCP server that proxies other MCP servers | 3 meta-tools | `catalog()` → `describe()` → `call()` | **none** — it is an ordinary server |

The sources differ (Codex: `codex-rs/tools/src/tool_discovery.rs`; OpenCode: `tool/code-mode.ts`; the Anthropic numbers are from its engineering blog; the rest are this repo's measurements), but they agree on the shape: **keep the resident set small and move the declarations behind a lookup**. lean-proxy's only distinguishing property is that it needs no host-side support at all — no experimental flag, no model capability, no schema field. That makes it the path that works when 3.1 silently does not.

**Pair it with a per-server denylist, not a blanket one.** Keeping the upstream's own `mcp__<server>__*` tools resident *and* proxying the same server pays for the declarations twice. Exclude exactly the servers in the proxy's `upstreams` (`mcp__kimi-cu__*`), and they stay callable through `call()`. This is compatible with 3.1's warning in a way `mcp__*` is not: the excluded server is reachable through the proxy, so removing it from the loadable catalogue removes nothing.

### 3.3 Delegate to specialist sub-agents (fallback)

The main agent drops the tool (`disallowedTools`) and a custom sub-agent carries it. Use this when deferral is unavailable (a model without `dynamically_loaded_tools`, a server that must stay inline) or when the sub-agent's distillation matters as much as the schema saving. The schema then only occupies the sub-agent's own context:

```yaml
# agents/agent.md (main, override)
disallowedTools:
  - mcp__<server>__*   # one line per server the sub-agent carries
  - ReadMediaFile
```

```yaml
# agents/mcp-worker.md (sub-agent)
tools:
  - mcp__*
  - Read
```

Once a server is behind the proxy (3.2), delegation stops being an access path and becomes an **output** choice: the caller can reach the tool itself, so spawn the worker only for results you do not want in the caller's context (screenshots, full accessibility trees, bulk queries). Keeping both roles is fine; describing delegation as "the way in" is not, because it hides the cheaper path.

Trade-offs:

- Every use of the excluded capability now costs a sub-agent spawn (its own prompt + tokens + latency). Do this for tools that are heavy and occasional, not for tools used every turn.
- Sub-agent results come back as text; media and huge outputs should be distilled by the sub-agent before returning.
- Write the sub-agent's `description` as routing advice — it is what the main agent reads when deciding to delegate.
- A sub-agent's `tools` list is not a hard boundary for `select_tools`, but it is for everything else. The loader is gated by `disallowedTools`, not by the `tools` allow-list, so whenever tool-select is active for that agent's own model — the model declares `dynamically_loaded_tools` and `tool_use`, and the flag is on — the runtime adds `select_tools` on top of the declared tools. Measured on `kimi-code/k3`: a sub-agent declaring `[mcp__*, Read, Grep, Glob]` resolved to `[Glob, Grep, Read, select_tools]`. An agent on a model without that capability gets exactly its declared list and no loader — measured on `deepseek/deepseek-flash`, where `web-researcher` resolved to `[FetchURL, Read, WebSearch]` and `mcp-worker` to its declared MCP tools plus `Read/Grep/Glob`. Do not read the list as an exhaustive allow-list for the loader; do rely on it for the rest.
- `disallowedTools` matches non-MCP names by exact membership and MCP names (`mcp__*`) by glob — `Tower*` would match nothing.

### 3.4 Moving tools out also helps caching

Prompt caching keys on a stable request prefix, and the tool array sits at the front of it. A tool list that shifts mid-session — an MCP server reconnecting, a schema appearing when it was absent before — invalidates everything cached behind it. Curating the resident tools (deferring MCP schemas, denying the low-frequency heavy ones) keeps the prefix stable and cache hits predictable, which is worth more than the one-off character saving. Native deferral is built for this: deferred definitions stay out of the cacheable prefix rather than being reordered inside it.

### 3.5 Cut or move? Session state has nowhere to go

Sections 3.1–3.3 share a hidden requirement: the work has a **destination**. Deferral re-loads the schema on demand; a proxy re-publishes it as a callable tool; delegation hands the task to another context that has the schema. All three work because the capability itself is stateless — it reads or changes something outside the session and returns a result.

A different class of tool has no destination: it acts on the **calling session**.

| Class | Examples | Disposition |
|---|---|---|
| Stateless capability | an MCP server, media reading, web fetch, file edits | Defer (3.1), proxy (3.2) or delegate (3.3) — the work happens elsewhere and comes back as a result |
| Session state | `CronCreate`/`CronDelete`/`CronList` (scheduled prompts), `CreateGoal`/`GetGoal`/`SetGoalBudget`/`UpdateGoal` (goal mode), `WaitFor` (waiting on background work), `EnterPlanMode`/`ExitPlanMode` | **Cut only** — no sub-agent can produce the outcome |

A reminder a sub-agent schedules is the sub-agent's, not the session's; a goal it sets is its own; a wait it performs blocks a context the user never sees. Nothing distilled comes back to the caller. So the only way to remove the schema is to remove the feature — `disallowedTools` — and the user trades a capability away for characters. That trade must be **explicit**: write down which features were cut, say the capability is gone rather than "delegated", and give the one-line way back (delete the entry). Do not describe a session-state tool as delegated; nothing is behind that word.

Which members to cut is a frequency call, not a class rule — plan mode sits in the same class, but users reach for it constantly, so cutting it usually costs more than its schema saves.

Measured caveat (CLI 0.43.1): a denied tool disappears from the **main** agent only. The built-in `coder` sub-profile still resolved to 29 tools (19 built-in + 10 `mcp__kimi-cu__*`) including `CronCreate`, `CronDelete`, `CronList` and `WaitFor`. Denying them upstream removes them from that prompt; it does not remove them from every profile on disk.

### 3.6 "Move it to a file" is not a loading mechanism — three classes

The natural follow-up to "keep it out of the resident prompt" is "put it in a file and let the agent read it". That works for some things and silently fails for others, and the deciding question is not size — it is **who performs the action**.

| Class | Examples | Does a file work? |
|---|---|---|
| Knowledge | specs, style guides, checklists, examples, house conventions | **Yes.** The file's content *is* the deliverable; reading it is the whole operation. This is what skill bodies and reference files are for. |
| Scripts | a CLI wrapper around an API, a data-conversion script, a generator, a test runner | **Yes, by execution.** The agent never needs the schema; it needs the entry point (`Bash: ./scripts/thing.py --help`) and the script's own `--help`. The capability lives outside the agent, and the file only records how to reach it. |
| Runtime session tools | `CronCreate`/`CronList`, `CreateGoal`/`UpdateGoal`, `WaitFor`, `EnterPlanMode`/`ExitPlanMode`, permission modes, the todo/task list | **No.** These are not content to read and not programs to run — they are capabilities the *host* must expose to the calling session. A file cannot make this session schedule a reminder, and a sub-agent reading the same file schedules its own. |

The middle class is the underused one: when a capability can be re-expressed as a script, moving it to a file genuinely removes the schema *and* keeps the capability. What it cannot do is re-declare a tool — only a server can do that, which is why 3.2 is about a *server*, not a file: the proxy's `catalog`/`describe`/`call` are real tools the runtime hands to the model, and `describe()` is exactly the "read the file when you need it" step applied to a tool declaration.

Read the three classes as a checklist before promising "it's still available": if the answer to "who executes this?" is "the session itself", a file is not a shipping container, and denying the tool removes the feature.


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
4. Also verify behavior, not just size: can the agent still reach the excluded capability through the intended path (proxy, delegation or `select_tools`)? For a **cut** feature there is no path — assert the tool is absent from the new session's `llm.tools_snapshot` and that the profile still holds the ones you chose to keep.
5. For the proxy path (3.2), assert reachability end to end, not just residency: the session's wire must show the meta-tools called in order (`catalog` → `describe` → `call`), and the call's result must be a real upstream answer. A prompt that only says "figure out which MCP tools you have" is the honest test — if the model needs a tool name from you, the path is not discoverable and the saving is not real.
6. Do the measurement in a throwaway environment (`KIMI_CODE_HOME` pointing at a temp home with `config.toml`/`oauth`/`credentials` symlinked, workdir under `/tmp`): the numbers then come from a session you can delete, and the user's own configuration never moves. Set `PYTHONDONTWRITEBYTECODE=1` so a run never leaves `__pycache__` behind in the repo.
