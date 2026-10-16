# docs

| 文档 | 用途 |
|------|------|
| [getting-started.md](getting-started.md) | 安装 → 首跑 → 故障速查 |
| [cli.md](cli.md) | 命令/参数速查（与 `--help` 对齐） |
| [manual.md](manual.md) | 运行机制契约（工作区、策略、记忆、循环） |
| [agent-loop-flow.md](agent-loop-flow.md) | 主循环控制流（mermaid；改 loop 必同步） |
| [bridge.md](bridge.md) | 聊天通道桥 |
| [mcp.md](mcp.md) | MCP 被调用 / 调用外挂 |
| [rationale.md](rationale.md) | 设计取舍（结论 + 指针，细节在 dogfooding/specs） |
| [benchmarking.md](benchmarking.md) / [roadmap.md](roadmap.md) | 基准与路线 |
| [handoff.md](handoff.md) | **交接：接下来要做的**（关口怎么用 / 已做完什么 / 待办清单） |
| [dogfooding.md](dogfooding.md) | 实测日志（**不精简**，按日期追加） |
| `superpowers/` | 历史设计/计划档 |

## 写法

1. **契约优先**：表 / 列表 / 命令块；少叙事。
2. **一页一责**：怎么用 ≠ 为什么；理由进 `rationale`，事故细节进 `dogfooding`。
3. **链出去**：重复说明用链接，不复制段落。
4. **术语稳定**：工作区 = 含 `.agent/`；机器级 = 不认 cwd；落盘 vs 待确认。
