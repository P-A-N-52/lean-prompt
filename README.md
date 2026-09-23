# lean-prompt

Kimi Code 插件：缩小常驻 prompt，把重型工具定义和长篇知识改为按需加载（渐进式披露）。

## 它做什么

实测一次普通会话（CLI 0.43.1，启用两个 MCP 插件）：常驻的「系统提示词 + 工具定义」约 **11.7 万字符**，其中 **92% 是工具定义**，Agent 指令本身不到 8%。本插件针对这一点：

| 组件 | 作用 |
|---|---|
| `agents/agent.md` | 覆盖默认主 Agent：从主 Agent 常驻工具中移除所有 `mcp__*`（MCP 工具）和 `ReadMediaFile`，其余全部保留；并追加一小段路由规则（Context budget rules） |
| `agents/mcp-worker.md` | 承接所有 MCP 调用的子 Agent（浏览器、桌面操作、数据查询等），schema 只在被委派时占用它自己的上下文 |
| `agents/web-researcher.md` | 多页面联网调研委派，只回传蒸馏结论 + 来源 |
| `agents/media-analyst.md` | 图片/视频理解委派，回传文字结论 |
| `skills/prompt-audit/` | 审计 Skill + 脚本：从会话 `wire.jsonl` 统计 prompt 各段大小、工具分组、逐请求 token |
| `skills/lean-context/` | 渐进式披露方法论（短正文 + `references/playbook.md` 长文，本身就是分层示范） |
| `commands/audit.md` | 斜杠命令 `/lean-prompt:audit` |

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

预期：常驻工具组中不再出现 `mcp:*` 和 `ReadMediaFile`；需要使用这些能力时，主 Agent 会自动委派对应子 Agent。

## 生效条件与优先级（重要）

主 Agent 瘦身依赖插件的 `agents/agent.md`（`name: agent, override: true`）。以下任一情况会**盖过**它，瘦身自动失效：

- 存在 `~/.kimi-code/SYSTEM.md`（用户级自定义主提示词，优先级高于插件 Agent）；
- 项目级 `.kimi-code/agents/agent.md` 声明了 `override: true`；
- 启动时使用了 `--agent` / `--agent-file`。

此时插件的子 Agent、Skills、命令仍然可用，只是主 Agent 恢复默认工具集。

## 如何调整

- **为主 Agent 加回某个工具**：编辑 `agents/agent.md` 的 `disallowedTools`（删除对应行），然后 `/plugins install` 重装 + `/reload`。
- **使用 tower 多代理模式**：自定义 Agent 文件会重新暴露默认隐藏的 8 个 Tower 编排工具（约 13.4k 字符），本插件已将其一并排除；如果你使用 `/tower`，从 `disallowedTools` 中删除 `Tower*` 条目。
- **经常直连某个 MCP server**：主 Agent 委派有额外 token/延迟成本。高频场景建议把该 server 从 `disallowedTools` 的 `mcp__*` 中例外出来（把通配改为逐个排除其他 server），或改用 MCP 实验性 `deferred: true`（见 `skills/lean-context/references/playbook.md` 第 3.2 节）。

## 卸载

```text
/plugins remove lean-prompt
```

卸载后新会话恢复默认主 Agent 与完整工具集。
