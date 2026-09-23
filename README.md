# lean-prompt

Kimi Code 插件：**保留全部工具能力**，只压缩「常驻描述」里最贵的那一块——MCP 工具的 schema。

做法不是删工具，而是把 MCP server 挪到 `lean-proxy` 后面：常驻的只有 `catalog()` / `describe()` / `call()` 三个紧凑元工具，每个上游的完整声明按名字现取。其余能力（包括 Cron*/Goal*/WaitFor、`AgentSwarm`、`ReadMediaFile`、`Tower*`）一律常驻可调用。

## 它做什么

实测一次普通会话（CLI 0.43.1，一个 MCP 插件）：常驻的「系统提示词 + 工具定义」约 **10.4 万字符**，其中 **91% 是工具定义**，Agent 指令本身不到 9%。其中单个 MCP server（kimi-cu，10 个工具）就占 8,151 字符。本插件针对这一点：

| 组件 | 作用 |
|---|---|
| `services/lean-proxy/` | MCP 代理（stdlib，无依赖）：把任意多个上游 MCP server 收成一个只暴露 `catalog()` / `describe()` / `call()` 的 server；**上游的每个工具都还能调，只是声明不常驻** |
| `agents/agent.md` | 全保真档（默认）：所有内建工具常驻；只精确排除**已被代理包裹的上游 server**（`mcp__kimi-cu__*`），避免同一份 schema 常驻两遍；并追加一小段路由规则（Context budget rules）说明「经代理先 catalog 再 describe 再 call」 |
| `agents/router.md` | router 档（实验性极限档）：只有 `Agent`/`Skill`/`Read`/`Bash` 4 个工具，自己是纯路由者，全部委派给子 Agent |
| `agents/mcp-worker.md` | 可选的**大输出隔离**手段：把截图、完整 AX 树、批量查询这类原始结果留在子 Agent 上下文里，只回传结论（不是 MCP 的通路，通路是代理） |
| `agents/web-researcher.md` | 可选的调研隔离：多页面调研只回传蒸馏结论 + 来源 |
| `agents/media-analyst.md` | 可选的媒体隔离：图片/视频理解只回传文字结论 |
| `skills/prompt-audit/` | 审计 Skill + 脚本：从会话 `wire.jsonl` 统计 prompt 各段大小、工具分组、逐请求 token |
| `skills/lean-context/` | 渐进式披露方法论（短正文 + `references/playbook.md` 长文，本身就是分层示范） |
| `commands/audit.md` | 斜杠命令 `/lean-prompt:audit` |

## 机制：三个元工具

```text
                     ┌─ lean-proxy ─────────────────────────────┐
  主 Agent ──stdio──┤ catalog() | describe(name) | call(n,args) ├──stdio/http──┬─ kimi-cu
                     └──────────────────────────────────────────┘               ├─ 浏览器
                                                                                └─ 任意 MCP server
```

| 元工具 | 作用 |
|---|---|
| `catalog()` | 列出所有上游工具：名字、所属 server、一行摘要（每条截到 `description_limit`，默认 120 字符） |
| `describe(name)` | 返回该工具的完整描述与 inputSchema，与上游声明逐字一致 |
| `call(name, arguments)` | 转发调用，原样返回上游结果（content 块、`structuredContent`、`isError` 都不改写） |

**没有任何工具被移除**：`call()` 能到达上游暴露的每一个工具，包括子 Agent 跑不了的有状态操作——模型只是先查名字。

## 实测效果

口径：所有常驻字符都是 utf-8 compact JSON（`prompt_audit.py` 的 `compact()`，即 `json.dumps(..., ensure_ascii=False, separators=(',',':'))`）；**常驻合计 = 系统提示词 + 工具定义数组**。单条工具的大小是「逐条相加」口径，数组口径 = 逐条相加 + (n+1)（n 个工具之间的 n−1 个分隔逗号，加两个方括号）。下面三组对照均为同机、同模型（`kimi-code/k3`）实测，每组的差异项在该小节里写明。

### 1. 同环境干净对照：直连常驻 vs 经代理（本版核心结论）

同一临时 home 里同时声明 `kimi-cu` 直连与 `lean` 代理，同一提示词，两个会话（`session_320c41b5` 用无 denylist 的 plain 主 Agent，`session_6c2451da` 用本插件的全保真档）：

| 指标 | 直连常驻（对照） | 全保真档 | 差异 |
|---|---:|---:|---:|
| 常驻字符（系统提示词 + 工具定义） | 118,053 | **112,178** | **−5,875（−5.0%）** |
| 其中工具定义 | 109,083 | **100,922** | −8,161 |
| 工具数 | 52 | 42 | −10 |
| 系统提示词 | 8,970 | 11,256 | +2,286（路由规则） |
| 首请求 input tokens | 27,013 | **25,625** | **−1,388（−5.1%）** |

两个会话只有主 Agent 一处不同：全保真档的 `disallowedTools` 把 10 个 `mcp__kimi-cu__*` 移出常驻（逐条合计 8,151 字符，数组口径 −8,161），代理的 3 个元工具两边都在（逐条合计 1,131 字符）。

把这样一个上游（10 个工具）**从直连改成经代理**，逐条口径的净账就是 **−8,151 + 1,131 = −7,020 字符（−86%）**：一个 server 的全部 schema，换成三个元工具。

### 2. 默认档、只把 MCP 通路换成代理（对照基线）

不装本插件主 Agent、只把 `kimi-cu` 从 `mcp.json` 挪进代理的 `config.json`（`session_4e016f41`，同目录、同提示词、同模型）：

| 指标 | 基线 `session_37fbf2b9`（kimi-cu 直连） | 经代理 `session_4e016f41` | 差异 |
|---|---:|---:|---:|
| 常驻字符 | 103,902 | **96,828** | **−7,074（−6.8%）** |
| 工具数 | 41 | 34 | −7 |
| 首请求 input tokens | 23,870 | **22,233** | **−1,637（−6.9%）** |

这一行的构成（逐条口径）：−8,151（10 条 kimi-cu 定义移出）+ 1,131（3 个元工具常驻）+ 185（本版改写的子 Agent 描述内嵌在 `Agent` 工具的 schema 里）− 232（提示词环境差异：工作目录名、目录清单、用户级 Skill 列表不同）= −7,067；数组口径再减 7 个分隔逗号，合计 −7,074。**上游越多省得越多**：再加一个上游，常驻增量是 **0**——它的工具只出现在 `catalog()` 的调用结果里，不是常驻 schema。

### 3. 相对基线的完整账（口径不同，逐项列出）

全保真档 `session_6c2451da` 比基线 `session_37fbf2b9` 多 8,276 字符 / 多 1,755 tokens——因为基线是**默认档**、且没有路由规则，而全保真档用自定义 Agent 文件（会把 8 个 Tower 工具重新暴露出来）。逐项（逐条口径）：

| 变化项 | 字符 |
|---|---:|
| 10 条 `mcp__kimi-cu__*` 直连定义移出常驻 | −8,151 |
| 3 个代理元工具常驻 | +1,131 |
| 8 个 Tower 工具（`TowerPlan`/`TowerSpawn`/`TowerMerge`/`TowerMission`/`TowerReview`/`TowerFinding`/`TowerSend`/`TowerInbox`；自定义 Agent 文件会重新暴露） | +13,069 |
| `Agent` 工具描述（内嵌子 Agent 名册，本版改写了 3 条子 Agent 描述） | +185 |
| 路由规则正文（prompt） | +2,041 |
| 工具定义数组净差（含分隔逗号） | 合计 +6,235 |

### 4. v0.3 → v0.4：把裁掉的能力加回来

v0.3 的省法不同：它把 `mcp__*`、`ReadMediaFile`、`AgentSwarm`、11 个 `Tower*`、8 个会话状态工具从主 Agent **移除**，换来 balanced 档 64,652 字符。代价是功能消失（`Cron*`/`Goal*`/`WaitFor` 作用于当前会话，没有委派通路）。v0.4 把这些全部恢复常驻：

| 恢复项 | 字符 |
|---|---:|
| 8 个会话状态工具（CronCreate/CronDelete/CronList/CreateGoal/GetGoal/SetGoalBudget/UpdateGoal/WaitFor） | +20,842 |
| 11 个 Tower 工具（TowerInit/TowerStatus/TowerTeardown + 上面 8 个） | +16,601 |
| AgentSwarm | +4,508 |
| ReadMediaFile | +4,120 |
| 小计（相对 v0.3 balanced） | **+46,071** |
| 新增：3 个代理元工具 | +1,131 |

版本对照（各版本各自实测，环境与工具清单不同，只作量级参考；常驻字符 = 系统提示词 + 工具定义）：

| 档位 / 版本 | 常驻字符 | 工具数 | 首请求 input tokens | 会话 |
|---|---:|---:|---:|---|
| 基线（默认档，kimi-cu 直连） | 103,902 | 41 | 23,870 | `session_37fbf2b9` |
| v0.2 balanced（裁剪版） | 85,277 | 26 | 19,682 | `session_473053ec` |
| v0.3 balanced（裁剪版） | 64,652 | 18 | 14,653 | `session_90c7b1cc` |
| v0.3 router（极简，`router.md` 本版未改） | 21,682 | 4 | 5,207 | `session_9c48afc9` |
| **v0.4 全保真档（本版默认主 Agent）** | **112,178** | 42 | 25,625 | `session_6c2451da` |
| v0.4（不用本插件主 Agent，只把 kimi-cu 换成经代理） | 96,828 | 34 | 22,233 | `session_4e016f41` |

v0.4 不是「更小」，而是「**同样的能力下更小**」：它把 v0.3 用删功能换来的 46,071 字符全部还回去，只保留 MCP 描述这一项压缩；如果你的 MCP server 很多，这一项的收益会持续变大，而 v0.3 的裁剪收益不会。

## 档位（profiles）

| | 全保真档（默认） | router（实验性极限档） |
|---|---|---|
| 文件 | `agents/agent.md` | `agents/router.md` |
| 启用方式 | 装插件即生效；或 `kimi --agent-file <插件>/agents/agent.md` | `kimi --agent router`；或 `kimi --agent-file <插件>/agents/router.md` |
| 常驻字符 / 工具数 | 112,178 / 42（见上表；随环境略有出入） | 21,682 / 4 |
| 工具 | 39 个内建工具（默认档下只暴露其中 31 个，另外 8 个 Tower 工具在自定义 Agent 文件下出现）+ 3 个代理元工具（`mcp__lean__catalog`/`describe`/`call`，插件声明时前缀为 `mcp__plugin-lean-prompt_lean__`） | `Agent` `Skill` `Read` `Bash` |
| 系统提示词 | 内建 base prompt + 路由规则 | 手写的路由正文，**不含** base prompt |
| 适合 | 日常使用：主 Agent 直接干活，MCP 经代理，重活/大输出才委派 | **实验性**：极简编排、省 token，主 Agent 不干活，全部委派 |

router 档是**纯路由者**：没有 `Edit`/`Write`/`Glob`/`Grep`/`WebSearch`，也没有任何 MCP 工具（连代理元工具都没有），所以任何实际工作都必须经过子 Agent——编码给 `coder`、MCP 给 `mcp-worker`、调研给 `web-researcher`、媒体给 `media-analyst`。它比全保真档小得多，但每件事都要多一次子 Agent 往返，且没有任何直连能力，**属于实验性档位**：默认不用，只有在「主 Agent 只编排、不执行」的场合才值得。

### 各能力的取值方式

| 能力 | 全保真档怎么取 | router 档怎么取 |
|---|---|---|
| MCP 工具 | 经代理 `catalog()` → `describe(name)` → `call(name, arguments)` | 委派 `mcp-worker` |
| 图片/视频 | 直接 `ReadMediaFile`；大媒体想隔离再委派 `media-analyst` | 委派 `media-analyst` |
| 多页面调研 | 直接 `WebSearch`/`FetchURL`；多页长文想隔离再委派 `web-researcher` | 委派 `web-researcher` |
| 批量并行 | 直接 `AgentSwarm` | 委派 `Agent`（没有 `AgentSwarm`） |
| 定时提醒 / goal 模式 / 等待后台任务 | 直接可用（`Cron*`/`Goal*`/`WaitFor`，作用于当前会话，无法委派） | 不可用 |
| 计划模式 / 提问 / 任务控制 | 直接可用 | 不可用（`EnterPlanMode`/`ExitPlanMode`/`AskUserQuestion`/`TodoList`/`TaskList`/`TaskOutput`/`TaskStop` 都不在） |

`mcp-worker`、`web-researcher`、`media-analyst` 在本版**不再是访问通路**，而是可选的「输出隔离」手段：主 Agent 自己就能调 MCP、读媒体、联网，只有当结果很大很脏（截图、完整 AX 树、几十页网页）时才值得多花一次子 Agent 往返把它们隔离出去。实测这一轮 E2E 里，主 Agent 用代理自己完成 `list_apps`（会话 `session_6c2451da`），没有委派。

## 安装

在 Kimi Code TUI 中（插件会把 MCP server `lean` 一并声明进来）：

```text
/plugins install /Users/pan/Desktop/kimi-code-prompt
/reload
```

然后**新建会话**生效（已打开的会话保留绑定时的 prompt）。

装好后必须做一件事：**把被包裹的原 server 从 `mcp.json` 里移出，写进代理自己的 `config.json`**，否则同一份 schema 会算两遍（插件默认只包 `kimi-cu`，见 `services/lean-proxy/config.json`；上游清单可自行增删）：

```text
~/.kimi-code/mcp.json            →  删掉 "kimi-cu"（它已成为上游）
services/lean-proxy/config.json  →  把 "kimi-cu" 写进 "upstreams"
```

- 你可以先用 `/plugins` 面板的 `M` 键看本插件声明的 MCP server（`lean`），必要时用 `/plugins mcp disable lean-prompt lean` 关掉。
- 如果某个 server 必须留在 `mcp.json`（例如别的客户端也在用同一份配置），全保真档的 `disallowedTools` 已经把它精确排除，不会重复常驻——但**新加的上游要自己补一行**（见下节「如何调整」）。
- 插件用相对路径声明代理：`{"command": "python3", "args": ["./server.py"], "cwd": "./services/lean-proxy"}`（CLI 会把 `cwd` 解析成插件根目录内的绝对路径，工作目录即 `services/lean-proxy`）。若你的环境里 `python3` 不在 PATH，改成绝对路径即可。

### 代理自己的配置（`services/lean-proxy/config.json`）

```json
{
  "timeout_seconds": 60,
  "startup_timeout_seconds": 15,
  "startup_wait_seconds": 25,
  "description_limit": 120,
  "upstreams": {
    "kimi-cu": {
      "command": "/Applications/KimiCU.app/Contents/MacOS/kimi-cu",
      "args": ["mcp", "-s", "user"]
    }
  }
}
```

字段含义、HTTP 上游、会话过期恢复、错误码等细节见 `services/lean-proxy/README.md`。单独调试代理（会连一遍所有上游并按模型看到的样子打印 catalog，任一下线则退 1）：

```sh
python3 services/lean-proxy/server.py --probe
python3 services/lean-proxy/server.py --probe --json
```

## 验证生效

新会话里执行 `/lean-prompt:audit`，或用脚本对比新旧会话：

```sh
python3 ~/.kimi-code/plugins/managed/lean-prompt/skills/prompt-audit/scripts/prompt_audit.py \
  --compare <旧会话目录> <新会话目录>
```

预期：

- 工具快照里 **没有** `mcp__kimi-cu__*`（被包裹的上游），**有** 3 个代理元工具；`Cron*`/`Goal*`/`WaitFor`/`AgentSwarm`/`ReadMediaFile`/`Tower*` 全部在；
- 常驻字符比同环境的直连会话少一个上游 schema 的量级（本机 10 工具约 −7,000 字符）；
- 新会话里问一句「用 MCP 列出正在运行的应用」，主 Agent 应自己走 `catalog()` → `describe()` → `call()`，不需要你告诉它工具名。

想自己量一遍（本仓库 E2E 的做法：全部落在 `/tmp`，不装到你的用户 home，也不动用户全局配置）：建一个临时 `KIMI_CODE_HOME`（symlink `config.toml`/`oauth`/`credentials`），写一个临时 `mcp.json`，再把 `agents/*.md` 拷进临时工作目录的 `.kimi-code/agents/`（子 Agent 从项目级目录被发现），然后：

```sh
cd /tmp/<workdir>
KIMI_CODE_HOME=/tmp/<home> PYTHONDONTWRITEBYTECODE=1 \
  kimi -p '<prompt>' --agent-file <插件>/agents/agent.md -m kimi-code/k3
```

注意 `--agent-file` 一次只能指定一个，且不能与 `--session`/`--continue` 同用。

## 生效条件与优先级（重要）

主 Agent 的配置依赖插件的 `agents/agent.md`（`name: agent, override: true`）。官方作用域序是 **Explicit > Project > Extra > User > Plugin > Built-in**，插件级**最低**，所以以下任意一项都会**盖过**它：

- `--agent` / `--agent-file` 启动（Explicit）；
- 项目级 `.kimi-code/agents/agent.md` 或 `.agents/agents/agent.md` 声明了 `override: true`（Project）；
- `config.toml` 的 `extra_agent_dirs`（TOML 字段用 snake_case，这是官方文档与 CLI 自己回写配置时的写法；实测 camelCase 也能被接受，但按文档写 snake_case）指向的目录里有同名 agent 文件（Extra）；
- 用户级 `~/.kimi-code/agents/agent.md` 或 `~/.agents/agents/agent.md`（User）；
- `~/.kimi-code/SYSTEM.md`：**未验证，以实测为准**。官方只说「项目级同名 override 文件与 `--agent-file` 排在 SYSTEM.md 之前」，同时说 agent 文件里的 `${base_prompt}` 会展开为有效默认（内建默认，或你的 SYSTEM.md）。按字面读，SYSTEM.md 存在时插件的 `agent.md` 可能**仍然是主 Agent**（denylist 继续生效），只是提示词来自 SYSTEM.md——也就是压缩 MCP 常驻不一定失效。装完请用 `/lean-prompt:audit` 实测确认。

被盖过时，插件的子 Agent、Skills、命令仍然可用，只是主 Agent 恢复默认工具集——此时 MCP 压缩就只剩「把 server 挪进代理」这一半（代理来自插件声明的 `lean` server，仍然生效），排除了重复常驻的那一行 denylist 则不再起作用。

`agents/router.md` 是 `override: false`：它不参与覆盖链，只作为一个**可被显式选中的档位**存在。用 `--agent router` 或 `--agent-file <插件>/agents/router.md` 才会进入 router 档（插件级目录下 `--agent router` 是否可发现尚未实测）。实测：把 `router.md` 放进项目级 `.kimi-code/agents/` 后 `kimi --agent router` 能解析出 `profileName=router`、4 个工具（`session_7bb551f1`）。

## 三条并行路径：只选一条

| 路径 | 需要什么 | 代价 |
|---|---|---|
| **lean-proxy（本插件默认）** | 什么都不需要——代理是普通 MCP server，模型不需要任何特殊能力 | 多一次 `catalog`/`describe` 往返；每上游工具调用多一层进程 |
| 原生延迟加载（`mcp.json` 里 `"deferred": true` + `select_tools`） | 实验开关 + 模型声明 `dynamically_loaded_tools` | 不需要子 Agent；但开关/模型不支持时静默失效 |
| 委派子 Agent（`mcp-worker`） | 什么都不需要，但每次都要 spawn | 每次调用一次子 Agent 往返；结果只能以文本回传。本版它已不是 MCP 的通路（通路是代理），只在你想把大输出挡在主上下文外时才用 |

前两条**互斥**（实测 CLI 0.43.1）：被 `disallowedTools` 排除的 MCP 工具会同时从「可加载目录」里消失——`select_tools` 只会回 `Unknown tool: mcp__kimi-cu__list_apps. Pick from the latest announced tools list.`（v0.2 实测它也不出现在 `<tools_added>` 公告里；本轮用 `session_49385ace` 复测了 `Unknown tool` 这一半）。顺带一个容易误解的点：已常驻的工具（包括代理的 3 个元工具）对 `select_tools` 同样回 `Unknown tool`——它们本来就在你的工具列表里，不需要加载。全保真档只排除**被代理包裹的那个 server**，所以：

- 想让某个 server 走原生延迟加载：**不要**把它写进代理的 `upstreams`，也**不要**把它加进 `disallowedTools`，保留 `deferred: true` 即可；
- 想让某个 server 走代理：加进 `upstreams`，并在 `disallowedTools` 里加一行 `mcp__<server>__*`；
- 不要对同一个 server 同时开两样。

## 如何调整

- **把更多 server 挂到代理后面**：写进 `services/lean-proxy/config.json` 的 `upstreams`，并在 `agents/agent.md` 的 `disallowedTools` 里加一行 `mcp__<server>__*`（已包裹的 server 名字取自 `upstreams` 的键），然后 `/plugins reload` + 新建会话。
- **加回某个被排除的上游**：删掉 `agents/agent.md` 里对应的 `mcp__<server>__*` 一行，它的 schema 就重新常驻（本机 kimi-cu 是 8,151 字符）。前提是它仍在 `mcp.json` 里；已挪进代理的 server 就算删了这行也不会常驻。
- **注意排除项的写法**：`disallowedTools` 只有 `mcp__*` 这类 MCP 名字支持通配，非 MCP 名字必须**逐个精确列出**（写 `Tower*` 不会匹配任何东西）。本插件只排除被包裹的 server，因此只会出现 `mcp__<server>__*` 形式的行；实测 `mcp__kimi-cu__*` 这种「带 server 名的通配」是生效的（会话 `session_6c2451da`：10 个工具全部消失，而代理的元工具仍在）。
- **使用 `/tower` 多代理模式**：Tower 工具本版不排除——自定义 Agent 文件下共 **11 个**（实测 16,601 字符，utf-8 compact JSON，本仓库 `prompt_audit.py` 口径），默认档下只暴露 `TowerInit`/`TowerStatus`/`TowerTeardown` 3 个（3,532 字符）。用 `/tower` 时不需要改任何东西；不想为此付 16,601 字符，就在 `disallowedTools` 里逐个列出这 11 个名字（注意不能用通配）。
- **切到 router 档**：`kimi --agent router`。它只有 4 个工具、没有 `select_tools`（本档在 `disallowedTools` 里显式排掉它：模型支持 tool-select 时运行时会把它叠加到 `tools` 之上，而 router 没有任何可加载工具，留着只会回 `Unknown tool`，实测 684 字符）。想让 router 直接调 MCP：把代理的 3 个元工具加进它的 `tools` 白名单（手写 `mcp.json` 时是 `mcp__lean__*`，插件声明时前缀为 `mcp__plugin-lean-prompt_lean__`）并删掉 `disallowedTools` 里的 `select_tools`（**未实测**，仅按同一个机制推断；代价约 +1,131 字符，仍远低于把上游 schema 放回去）。
- **大输出隔离**：把原委派给 `mcp-worker` 的说法换成「结果很大很脏时才委派」——通路本身已经是代理。
- **不用插件、只要 MCP 压缩**：把 `lean-proxy` 直接写进 `mcp.json`，并删掉被包裹的 server（实测省 7,074 字符 / 1,637 tokens，`session_4e016f41`）。这就是上面第 2 张表。

## 卸载

```text
/plugins remove lean-prompt
```

卸载后新会话恢复默认主 Agent 与完整工具集（`router` 档同时消失，因为它的文件来自本插件）；被代理包裹的 server 仍在代理的 `config.json` 里，要恢复直连就把它们写回 `mcp.json` 并去掉代理。
