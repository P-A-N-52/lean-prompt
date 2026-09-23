# lean-prompt

Kimi Code 插件：缩小常驻 prompt，把重型工具定义和长篇知识改为按需加载（渐进式披露）。

## 它做什么

实测一次普通会话（CLI 0.43.1，一个 MCP 插件）：常驻的「系统提示词 + 工具定义」约 **10.4 万字符**，其中 **91% 是工具定义**，Agent 指令本身不到 9%。本插件针对这一点：

| 组件 | 作用 |
|---|---|
| `agents/agent.md` | 覆盖默认主 Agent：移除 `mcp__*`（MCP 工具）、`ReadMediaFile`、`AgentSwarm` 和 Tower 编排工具，其余全部保留（Cron*/AskUserQuestion/Task*/Goal*/EnterPlanMode/ExitPlanMode 等会话状态工具一个不动）；并追加一小段路由规则（Context budget rules） |
| `agents/mcp-worker.md` | 承接所有 MCP 调用的子 Agent（浏览器、桌面操作、数据查询等），schema 只在被委派时占用它自己的上下文 |
| `agents/web-researcher.md` | 多页面联网调研委派，只回传蒸馏结论 + 来源 |
| `agents/media-analyst.md` | 图片/视频理解委派，回传文字结论 |
| `skills/prompt-audit/` | 审计 Skill + 脚本：从会话 `wire.jsonl` 统计 prompt 各段大小、工具分组、逐请求 token |
| `skills/lean-context/` | 渐进式披露方法论（短正文 + `references/playbook.md` 长文，本身就是分层示范） |
| `commands/audit.md` | 斜杠命令 `/lean-prompt:audit` |

## 实测效果

同工作目录、同模型（`kimi-code/k3`）、同提示词，只换主 Agent，用插件自带脚本统计：

| 指标 | 默认主 Agent | 启用本插件 | 变化 |
|---|---:|---:|---:|
| 常驻字符（系统提示词 + 工具定义） | 103,902 | 85,277 | **−18,625（−17.9%）** |
| 其中工具定义 | 94,687 | 74,361 | −20,326 |
| 工具数 | 41 | 26 | −15 |
| 首请求 input tokens | 23,870 | 19,682 | **−4,188（−17.5%）** |

被移出的部分：10 个 `mcp__kimi-cu__*`（8,151 字符）、`ReadMediaFile`（4,120）、`AgentSwarm`（4,508）、Tower 工具 3 个（TowerInit/TowerStatus/TowerTeardown，共 3,532）；新增的常驻路由规则 +1,699 字符，所以净省略小于工具定义的减少量。收益随 MCP server 数量增长——每多一个 server，它的全部 schema 都不再常驻。

本页所有字符数都是 utf-8 compact JSON（`prompt_audit.py` 的 `compact()`，即 `json.dumps(..., ensure_ascii=False, separators=(',', ':'))`）。同样一份工具定义改用 ascii-escaped（`ensure_ascii=True`）会略大——例如 11 个 Tower 工具是 16,601 对 16,747。对比数字时注意口径。

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

预期：常驻工具组中不再出现 `mcp:*`、`ReadMediaFile`、`AgentSwarm`；需要使用这些能力时，主 Agent 会自动委派对应子 Agent。

想自己量一遍：建一个临时目录 `git init`，把 `agents/*.md` 拷到 `.kimi-code/agents/`（子 Agent 从项目级目录被发现），然后用 `kimi -p '<prompt>'` 和 `kimi -p '<prompt>' --agent-file <插件>/agents/agent.md` 各跑一次（注意 `--agent-file` 一次只能指定一个），再用 `--compare` 对比这两个会话目录。

## 生效条件与优先级（重要）

主 Agent 瘦身依赖插件的 `agents/agent.md`（`name: agent, override: true`）。官方作用域序是 **Explicit > Project > Extra > User > Plugin > Built-in**，插件级**最低**，所以以下任意一项都会**盖过**它：

- `--agent` / `--agent-file` 启动（Explicit）；
- 项目级 `.kimi-code/agents/agent.md` 或 `.agents/agents/agent.md` 声明了 `override: true`（Project）；
- `config.toml` 的 `extra_agent_dirs`（TOML 字段用 snake_case，这是官方文档与 CLI 自己回写配置时的写法；实测 camelCase 也能被接受，但按文档写 snake_case）指向的目录里有同名 agent 文件（Extra）；
- 用户级 `~/.kimi-code/agents/agent.md` 或 `~/.agents/agents/agent.md`（User）；
- `~/.kimi-code/SYSTEM.md`：**未验证，以实测为准**。官方只说「项目级同名 override 文件与 `--agent-file` 排在 SYSTEM.md 之前」，同时说 agent 文件里的 `${base_prompt}` 会展开为有效默认（内建默认，或你的 SYSTEM.md）。按字面读，SYSTEM.md 存在时插件的 `agent.md` 可能**仍然是主 Agent**（denylist 继续生效），只是提示词来自 SYSTEM.md——也就是瘦身不一定失效。装完请用 `/lean-prompt:audit` 实测确认。

被盖过时，插件的子 Agent、Skills、命令仍然可用，只是主 Agent 恢复默认工具集。

## 原生延迟加载 vs 本插件：二选一，别混用

Kimi Code 有实验性的 MCP 延迟加载：`mcp.json` 里给 server 加 `"deferred": true`，它的 schema 就不进常驻 `tools[]`，模型用 `select_tools` 按需拉取——不用委派、没有额外 spawn 成本。**能用就用它**，本插件的 `disallowedTools` + 委派是模型不支持 `dynamically_loaded_tools`、或 server 必须内联时的兜底。

两条路**互斥**，已实测（CLI 0.43.1）：被 `disallowedTools: [mcp__*]` 排除的 MCP 工具会同时从「可加载目录」里消失——它不出现在 `<tools_added>` 公告里，`select_tools` 只会回 `Unknown tool`。也就是说装了本插件之后，某个 server 即使写了 `deferred: true` 也**永远拿不回来**。

所以按 server 做选择：要原生延迟，就把该 server 从 `mcp__*` 通配里摘出去（改成逐个排除其他 server）并保留 `deferred: true`；要委派，就让它留在通配里。细节见 `skills/lean-context/references/playbook.md` 第 3 节。

## 如何调整

- **为主 Agent 加回某个工具**：编辑 `agents/agent.md` 的 `disallowedTools`（删除对应行），然后 `/plugins install` 重装 + `/reload`。
- **加回 `AgentSwarm`**（批量并行编排）：删掉 `- AgentSwarm` 一行；它的 schema 实测 4,508 字符，低频，建议确实要用时再加回。
- **加回 `ReadMediaFile`**：删掉该行（实测 4,120 字符），加回后主 Agent 可直接读图，不必再走 `media-analyst`。
- **使用 `/tower` 多代理模式**：自定义 Agent 文件会把默认隐藏的 Tower 编排工具重新暴露——CLI 0.43.1 下共 **11 个**，实测合计 **16,601 字符**（utf-8 compact JSON，即本仓库 `prompt_audit.py` 的口径）。其中 TowerInit/TowerStatus/TowerTeardown 连默认 profile 都不隐藏：M3 审查报告只量到 8 个（13,069 字符，同一口径），差值 3,532 正好是这 3 个。本插件已一并排除；用 `/tower` 时请从 `disallowedTools` 删掉这些 `Tower*` 条目。
- **注意排除项的写法**：`disallowedTools` 只有 `mcp__*` 这类 MCP 名字支持通配，非 MCP 名字必须**逐个精确列出**（写 `Tower*` 不会匹配任何东西）。CLI 新增工具时会静默漏出，留意上面这些实测数字是否漂移。
- **经常直连某个 MCP server**：见上一节，按 server 二选一。

## 卸载

```text
/plugins remove lean-prompt
```

卸载后新会话恢复默认主 Agent 与完整工具集。
