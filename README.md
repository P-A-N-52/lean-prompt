# lean-prompt

Kimi Code 插件：缩小常驻 prompt，把重型工具定义和长篇知识改为按需加载（渐进式披露）。

## 它做什么

实测一次普通会话（CLI 0.43.1，一个 MCP 插件）：常驻的「系统提示词 + 工具定义」约 **10.4 万字符**，其中 **91% 是工具定义**，Agent 指令本身不到 9%。本插件针对这一点：

| 组件 | 作用 |
|---|---|
| `agents/agent.md` | balanced 档（默认）：覆盖默认主 Agent，移除 `mcp__*`（MCP 工具）、`ReadMediaFile`、`AgentSwarm`、Tower 编排工具，以及 Cron*/Goal*/WaitFor 这 8 个会话状态工具；编码/读写/检索/计划/任务控制工具全部保留（EnterPlanMode/ExitPlanMode/AskUserQuestion/TodoList/TaskList/TaskOutput/TaskStop）；并追加一小段路由规则（Context budget rules） |
| `agents/router.md` | router 档（极简）：只有 `Agent`/`Skill`/`Read`/`Bash` 4 个工具，自己是纯路由者，不持有任何编辑/MCP/联网能力，全部委派给子 Agent |
| `agents/mcp-worker.md` | 承接所有 MCP 调用的子 Agent（浏览器、桌面操作、数据查询等），schema 只在被委派时占用它自己的上下文 |
| `agents/web-researcher.md` | 多页面联网调研委派，只回传蒸馏结论 + 来源 |
| `agents/media-analyst.md` | 图片/视频理解委派，回传文字结论 |
| `skills/prompt-audit/` | 审计 Skill + 脚本：从会话 `wire.jsonl` 统计 prompt 各段大小、工具分组、逐请求 token |
| `skills/lean-context/` | 渐进式披露方法论（短正文 + `references/playbook.md` 长文，本身就是分层示范） |
| `commands/audit.md` | 斜杠命令 `/lean-prompt:audit` |

## 实测效果

同工作目录、同模型（`kimi-code/k3`）、同提示词，只换主 Agent，用插件自带脚本统计：

| 指标 | 默认主 Agent（基线） | balanced 档 | router 档 |
|---|---:|---:|---:|
| 常驻字符（系统提示词 + 工具定义） | 103,902 | 64,652 | 21,682 |
| 其中工具定义 | 94,687 | 53,511 | 20,250 |
| 工具数 | 41 | 18 | 4 |
| 首请求 input tokens | 23,870 | 14,653 | 5,207 |

相对基线：balanced 档 **−39,250 字符（−37.8%）/ −9,217 tokens（−38.6%）**，router 档 **−82,220 字符（−79.1%）/ −18,663 tokens（−78.2%）**。

被移出的部分（均以基线会话逐工具实测）：10 个 `mcp__kimi-cu__*`（8,151 字符）、`ReadMediaFile`（4,120）、`AgentSwarm`（4,508）、Tower 工具 3 个（TowerInit/TowerStatus/TowerTeardown，共 3,532）、**v0.3 新增的 8 个会话状态工具（CronCreate/CronDelete/CronList/CreateGoal/GetGoal/SetGoalBudget/UpdateGoal/WaitFor，合计 20,842）**；新增的常驻路由规则 +1,926 字符（v0.2 已有 +1,701，v0.3 再加一句会话状态说明），所以净省略小于工具定义的减少量。收益随 MCP server 数量增长——每多一个 server，它的全部 schema 都不再常驻。

版本对照：v0.2 的 balanced 档是 85,277 字符 / 26 工具 / 19,682 tokens；v0.3 再摘掉上面那 8 个会话状态工具（−20,850 字符数组口径 / −5,029 tokens），并新增 router 档。

本页所有字符数都是 utf-8 compact JSON（`prompt_audit.py` 的 `compact()`，即 `json.dumps(..., ensure_ascii=False, separators=(',', ':'))`）。同样一份工具定义改用 ascii-escaped（`ensure_ascii=True`）会略大——例如 11 个 Tower 工具是 16,601 对 16,747。对比数字时注意口径。

## 档位（profiles）

插件带两个档位。**balanced 是默认**（装插件即生效）；**router 更极简，需要显式启用**。

| | balanced | router |
|---|---|---|
| 文件 | `agents/agent.md` | `agents/router.md` |
| 启用方式 | 装插件即生效；或 `kimi --agent-file <插件>/agents/agent.md` | `kimi --agent router`；或 `kimi --agent-file <插件>/agents/router.md` |
| 常驻字符 / 工具数 | 64,652 / 18 | 21,682 / 4 |
| 工具 | `Agent` `Skill` `Read` `Bash` `Edit` `Write` `Glob` `Grep` `WebSearch` `FetchURL` `TodoList` `TaskList` `TaskOutput` `TaskStop` `AskUserQuestion` `EnterPlanMode` `ExitPlanMode` `select_tools` | `Agent` `Skill` `Read` `Bash` |
| 系统提示词 | 内建 base prompt + 路由规则 | 手写的路由正文，**不含** base prompt |
| 适合 | 日常使用：主 Agent 直接干活，重活才委派 | 极简编排、省 token：主 Agent 不干活，全部委派 |

router 档是**纯路由者**：它没有 `Edit`/`Write`/`Glob`/`Grep`/`WebSearch`，也没有任何 MCP 工具，所以任何实际工作都必须经过子 Agent——编码给 `coder`、MCP 给 `mcp-worker`、调研给 `web-researcher`、媒体给 `media-analyst`，主 Agent 只回传蒸馏后的结论。一句话：balanced 省的是「重型工具的 schema」，router 省的是「几乎所有工具的 schema」，代价是每一件事都要多一次子 Agent 往返。

### 被摘掉的功能与加回方法

`Cron*`/`Goal*`/`WaitFor` 属于**会话状态**类工具——它们作用于**发起调用的那个会话**，交给子 Agent 没有意义（子 Agent 设的 goal 是它自己的，它排的定时任务是它的）。所以这类工具**无法委派，只能裁剪，裁剪就等于功能消失**，是用户的取舍。逐项：

| 被摘功能 | 工具 | 在哪些档位消失 | 加回方法 |
|---|---|---|---|
| 定时提醒 / 周期任务 | CronCreate, CronDelete, CronList | 两档都消失 | balanced：删掉 `agents/agent.md` 里 3 行 `Cron*`；router：用 balanced 档（或把 `- CronCreate` 等加进 router 的 `tools`，并删掉 `disallowedTools` 的 `select_tools`） |
| goal 模式（`/goal`） | CreateGoal, GetGoal, SetGoalBudget, UpdateGoal | 两档都消失 | 同上，删掉 `agents/agent.md` 里 4 行 `Goal*` |
| 等待后台任务 | WaitFor | 两档都消失 | 同上，删掉 `- WaitFor` 一行 |
| 计划模式 | EnterPlanMode, ExitPlanMode | **仅 router 档**消失 | 用 balanced 档（balanced 保留计划模式） |
| MCP 工具 | `mcp__*` | 两档都消失 | 委派 `mcp-worker`；或按 server 改用原生延迟加载（见下节） |
| 图片/视频读取 | ReadMediaFile | 两档都消失 | 委派 `media-analyst`；或删掉 `- ReadMediaFile` 一行 |
| 批量并行编排 | AgentSwarm | 两档都消失 | 删掉 `- AgentSwarm` 一行（4,508 字符） |
| Tower 编排 | Tower*（11 个） | 两档都消失 | 用 `/tower` 时删掉这些 `Tower*` 条目（16,601 字符） |

任务控制类工具（`TaskList`/`TaskOutput`/`TaskStop`）与 `AskUserQuestion`、`TodoList` 在 **balanced 档保留**：`TaskStop` 是取消后台任务的唯一手段，摘掉后台任务就没法取消；计划模式用户实际在用，所以 `EnterPlanMode`/`ExitPlanMode` 也保留。这些连同计划模式**只有 router 档去掉**，因为 router 不自己执行任何东西。

一个实测到的边界：「功能消失」指的是**这个主 Agent 的**工具快照里不再有它。内建 `coder` 子档实测仍有 29 个工具（19 内建 + 10 个 `mcp__kimi-cu__*`），其中包含 `Cron*` 与 `WaitFor`（`session_fee0d796` 的 `agent-0`）——同一台机器上别的 profile 不受本档影响。balanced 档的提示词已明确要求不要为这类请求绕道委派，本插件也不把这条路径当作支持用法。

改完 `agents/*.md` 后需要 `/plugins install` 重装 + `/reload`，并**新建会话**生效。

## 安装

在 Kimi Code TUI 中：

```text
/plugins install /Users/pan/Desktop/kimi-code-prompt
/reload
```

然后**新建会话**生效（已打开的会话保留绑定时的 prompt）。

## 验证生效

新会话里执行 `/lean-prompt:audit`，或用脚本对比新旧会话：

```sh
python3 ~/.kimi-code/plugins/managed/lean-prompt/skills/prompt-audit/scripts/prompt_audit.py \
  --compare <旧会话目录> <新会话目录>
```

预期：balanced 档下常驻工具组中不再出现 `mcp:*`、`ReadMediaFile`、`AgentSwarm`、`Tower*`、`Cron*`、`Goal*`、`WaitFor`，而 `EnterPlanMode`/`ExitPlanMode`/`AskUserQuestion`/`TodoList`/`TaskList`/`TaskOutput`/`TaskStop` 仍在；需要使用被摘能力时，主 Agent 会委派对应子 Agent。

两档各自的实测（v0.3，对照基线 `session_37fbf2b9`，均为新会话、同模型 `kimi-code/k3`、同提示词）：

| 断言 | balanced `session_90c7b1cc` | router `session_9c48afc9` |
|---|---|---|
| 工具快照 | 18 个；无 `CronCreate`/`CronDelete`/`CronList`/`CreateGoal`/`GetGoal`/`SetGoalBudget`/`UpdateGoal`/`WaitFor` | 4 个：`Agent` `Read` `Bash` `Skill` |
| 保留项 | `EnterPlanMode`/`ExitPlanMode`/`AskUserQuestion`/`TodoList`/`TaskList`/`TaskOutput`/`TaskStop` 全部在 | 不适用（router 不自己干活） |
| 委派 | `mcp-worker`/`web-researcher`/`media-analyst` 可用 | 实测委派 `coder` 写出真实文件并回传结论（`session_fee0d796`：主 Agent 只调 `Agent`，`agent-0` 的 `profileName=coder`） |

想自己量一遍：建一个临时目录 `git init`，把 `agents/*.md` 拷到 `.kimi-code/agents/`（子 Agent 从项目级目录被发现），然后用 `kimi -p '<prompt>' -m <model>`、加上 `--agent-file <插件>/agents/agent.md`（balanced 档）或 `--agent-file <插件>/agents/router.md`（router 档）各跑一次（注意 `--agent-file` 一次只能指定一个，且不能与 `--session`/`--continue` 同用），再用 `--compare` 对比这几个会话目录。

## 生效条件与优先级（重要）

主 Agent 瘦身依赖插件的 `agents/agent.md`（`name: agent, override: true`）。官方作用域序是 **Explicit > Project > Extra > User > Plugin > Built-in**，插件级**最低**，所以以下任意一项都会**盖过**它：

- `--agent` / `--agent-file` 启动（Explicit）；
- 项目级 `.kimi-code/agents/agent.md` 或 `.agents/agents/agent.md` 声明了 `override: true`（Project）；
- `config.toml` 的 `extra_agent_dirs`（TOML 字段用 snake_case，这是官方文档与 CLI 自己回写配置时的写法；实测 camelCase 也能被接受，但按文档写 snake_case）指向的目录里有同名 agent 文件（Extra）；
- 用户级 `~/.kimi-code/agents/agent.md` 或 `~/.agents/agents/agent.md`（User）；
- `~/.kimi-code/SYSTEM.md`：**未验证，以实测为准**。官方只说「项目级同名 override 文件与 `--agent-file` 排在 SYSTEM.md 之前」，同时说 agent 文件里的 `${base_prompt}` 会展开为有效默认（内建默认，或你的 SYSTEM.md）。按字面读，SYSTEM.md 存在时插件的 `agent.md` 可能**仍然是主 Agent**（denylist 继续生效），只是提示词来自 SYSTEM.md——也就是瘦身不一定失效。装完请用 `/lean-prompt:audit` 实测确认。

被盖过时，插件的子 Agent、Skills、命令仍然可用，只是主 Agent 恢复默认工具集。

`agents/router.md` 是 `override: false`：它不参与覆盖链，只作为一个**可被显式选中的档位**存在。用 `--agent router` 或 `--agent-file <插件>/agents/router.md` 才会进入 router 档；不选它时它不产生任何影响（也因此没有「被盖过」的问题）。实测：把 `router.md` 放进项目级 `.kimi-code/agents/` 后 `kimi --agent router` 能解析出 `profileName=router`、4 个工具（`session_7bb551f1`）。插件级目录下 `--agent router` 是否同样可发现尚未实测（本仓库未安装插件自测），装完请用 `/lean-prompt:audit` 确认。

## 原生延迟加载 vs 本插件：二选一，别混用

Kimi Code 有实验性的 MCP 延迟加载：`mcp.json` 里给 server 加 `"deferred": true`，它的 schema 就不进常驻 `tools[]`，模型用 `select_tools` 按需拉取——不用委派、没有额外 spawn 成本。**能用就用它**，本插件的 `disallowedTools` + 委派是模型不支持 `dynamically_loaded_tools`、或 server 必须内联时的兜底。

两条路**互斥**，已实测（CLI 0.43.1）：被 `disallowedTools: [mcp__*]` 排除的 MCP 工具会同时从「可加载目录」里消失——它不出现在 `<tools_added>` 公告里，`select_tools` 只会回 `Unknown tool`。也就是说装了本插件之后，某个 server 即使写了 `deferred: true` 也**永远拿不回来**。

所以按 server 做选择：要原生延迟，就把该 server 从 `mcp__*` 通配里摘出去（改成逐个排除其他 server）并保留 `deferred: true`；要委派，就让它留在通配里。细节见 `skills/lean-context/references/playbook.md` 第 3 节。

## 如何调整

- **为主 Agent 加回某个工具**：编辑 `agents/agent.md` 的 `disallowedTools`（删除对应行），然后 `/plugins install` 重装 + `/reload`。
- **加回 `AgentSwarm`**（批量并行编排）：删掉 `- AgentSwarm` 一行；它的 schema 实测 4,508 字符，低频，建议确实要用时再加回。
- **加回 `ReadMediaFile`**：删掉该行（实测 4,120 字符），加回后主 Agent 可直接读图，不必再走 `media-analyst`。
- **加回定时提醒 / goal 模式 / 后台等待**：这三类各是 3 行 `Cron*`、4 行 `Goal*`、1 行 `WaitFor` 的 `disallowedTools` 条目（合计实测 20,842 字符），删掉对应行即可。它们**没有委派通路**——工具作用于当前会话，子 Agent 做的不是你要的结果，所以加回就是唯一的恢复方式。
- **切到 router 档**：`kimi --agent router`。它只有 4 个工具、没有 `select_tools`（本档在 `disallowedTools` 里显式排掉它：模型支持 tool-select 时运行时会把它叠加到 `tools` 之上，而 router 没有任何可加载工具，留着只会回 `Unknown tool`，实测 684 字符）。想让 router 自己拿某能力（例如加 `WebSearch`）：把它加进 `tools` 白名单并在 `disallowedTools` 里删掉 `select_tools`；想让 router 用上定时提醒/goal：改用 balanced 档。要求 router 具备这些能力，等于放弃它「纯路由」的前提。
- **使用 `/tower` 多代理模式**：自定义 Agent 文件会把默认隐藏的 Tower 编排工具重新暴露——CLI 0.43.1 下共 **11 个**，实测合计 **16,601 字符**（utf-8 compact JSON，即本仓库 `prompt_audit.py` 的口径）。其中 TowerInit/TowerStatus/TowerTeardown 连默认 profile 都不隐藏：M3 审查报告只量到 8 个（13,069 字符，同一口径），差值 3,532 正好是这 3 个。本插件已一并排除；用 `/tower` 时请从 `disallowedTools` 删掉这些 `Tower*` 条目。
- **注意排除项的写法**：`disallowedTools` 只有 `mcp__*` 这类 MCP 名字支持通配，非 MCP 名字必须**逐个精确列出**（写 `Tower*` 不会匹配任何东西）。CLI 新增工具时会静默漏出，留意上面这些实测数字是否漂移。
- **经常直连某个 MCP server**：见上一节，按 server 二选一。

## 卸载

```text
/plugins remove lean-prompt
```

卸载后新会话恢复默认主 Agent 与完整工具集（`router` 档同时消失，因为它的文件来自本插件）。
