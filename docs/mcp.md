# MCP

## 被调用：`spool mcp`

外部 agent（Cursor / Codex / …）编排，本进程在工作区实施。

```json
{
  "mcpServers": {
    "spool": {
      "command": "spool",
      "args": ["mcp", "--scope", "src", "--policy", "auto"]
    }
  }
}
```

| 参数 | 含义 |
|------|------|
| `--scope` / `--policy` | 自动落盘范围 / 授权档 |
| 无 `--root` | 机器级；调用带 `workspace`（须已有 `.agent/`） |
| 有 `--root` | 钉死；点名其它工作区报错 |
| 多工作区且无 `workspace` | `delegate_task` 报错列候选，不猜 |

`fake` 不能当生成器。工作区只能人建（`spool ws add`）。

**信任**：`confirm_changes(apply=true)` ≡ 用户点「应用」。边界靠 scope/policy，不靠协议层。

### 工具

| 工具 | 作用 |
|------|------|
| `delegate_task` | 立即返回 `task_id`（`goal` 必填；`mode`: task/autonomous/plan） |
| `task_status` / `task_events` | 快照 / 按 `since` 拉事件 |
| `confirm_changes` | 审批落盘 |
| `workspace_status` / `list_workspaces` / `list_sessions` | 只读状态 |

多 Runner：`(工作区, 会话)`；status/events/confirm 作用在**最近一次** `delegate_task`。

### 序列

```
initialize → tools/list
list_workspaces → delegate_task → task_events(since=0)…
task_status → confirm_changes{apply}
```

事件约 400 条 + seq；可选 `_meta.progressToken` 推送。理由 → [rationale.md](rationale.md#异步与协议)。

---

## 调用外挂：`[[mcp]]`

```toml
[[mcp]]
name = "filesystem"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", 'D:\work']
```

Windows 路径：`"D:\\work"` 或 `'D:\work'`。校验：`spool mcp-servers --check`；临时排除：`--no-mcp`。

| 规则 | |
|------|--|
| 命名 | `mcp__<服务>__<工具>` |
| 可见性 | 仅主循环（只读角色白名单不含外挂） |
| 失败 | 跳过并明示，不静默降级 |
| 信任 | 外挂权限由对方服务决定；挂载即授权 |
