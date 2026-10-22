# 消息通道桥

聊天消息 → 一次 agent `run` → 回发结果。授权/工作区/待确认与 CLI、网页壳共用。

## 通道

| 通道 | 收 | 发 | 备注 |
|------|----|----|------|
| `fake` | stdin | stdout | 无凭据；测桥。≠ `--provider fake` |
| `qqbot` | WS 网关（无需公网入口） | REST 消息 | AppID + AppSecret |
| `telegram` | 长轮询 | `sendMessage` | bot token；国内常需代理 |
| `wecom` | 回调（需公网+解密） | `message/send` | 本版解密/入口在部署层 |

## 凭据

顺序：env → 工作区 `.env` → 用户级 `.env`。机器级（无 `--root`）无工作区层。

```powershell
spool env set QQ_AppID=… QQ_AppSecret=…
spool bridge --channel qqbot --check    # 机器级可用；打印找过的位置（键名/遮罩）
```

`--bridge-proxy` = 通道出网；`--proxy` = 模型供应商。SOCKS 需 `httpx[socks]`。QQ IP 白名单可用 `ssh -D` 云主机。

## `[channel.<名>]`（仅短命令）

```toml
[channel.qqbot]
proxy = "socks5://127.0.0.1:1080"   # = --bridge-proxy
policy = "ask"
scope = "**"
timeout = 240
web = true
```

层：CLI > `SPOOLKIT_CHANNEL_<名>_*` > 用户配置 > 默认。`spool qq` 读此表；`bridge`/`hub` 默认不变。`root` 认但不读。启动时 stderr 打印生效值；非法键/值跳过该层并明示。

## 安全

1. **默认配对**：`spool approve <码>`；档位 `pairing` / `allowlist` / `open`（open 须显式）
2. **策略透传**：`--policy`/`--scope` 进子进程；越界仍确认
3. **长度上限**：默认 4000（`--max-chars`）；超长不交 agent

信任存机器级 `channel-trust.json`（身份级，换工作区不重配）。

| 操作 | 命令 |
|------|------|
| 列表/批准/撤销/丢码 | `spool approve --list` / `<码>` / `--revoke` / `--forget` |
| 旧码指定工作区 | `--ws <名字>` |
| 钉死工作区 | 启动 `--root` |

### 斜杠（放行后）

`/ws` `/ws use` `/sessions` `/use` `/new` `/status` `/help` — 只改指向/会话。  
授权、`extra`、`revert` 不在聊天开放。配对在本机 `approve` 或 `spool chat` 的 `/approve`。

### 运行时

| 情形 | 行为 |
|------|------|
| 超过 `--timeout` | 先回执，结束后补发结论 |
| 同工作区连发 | 第二轮拒绝直至上一轮结束 |
| 长结论 | 按 ~3000 字节分条 + `msg_seq` |
| 待确认 / 命令授权 | `y/n`；命令另有 `a/b` |

闲聊也会跑完整一轮 agent。

### 渲染

| 模式 | 通道 | 规则 |
|------|------|------|
| `fix` | QQ | 去掉不渲染的强调标记；其它保留 |
| `plain` | 企微 / 无 parse_mode 的 TG | 折纯文本 |
| `markdown` | 真 MD 通道 | 不动 |

只动标记，不改代码/标识符内容。→ [rationale.md](rationale.md#长连接与渲染)

## 已知边界

- QQ：`author`=openid；被动回复 5min `msg_id`；`RESUME` 未实现（短抖不丢，长断可能漏）
- 仅文本消息
- 企微 AES 回调未实现
- 桥内串行；网络失败可暂存补发，业务错误（如 11001）不暂存
- 不做个人微信/QQ 第三方 hook

## 新通道

实现 `Channel`：`send` / `poll`（非阻塞空列表）/ `close`。装载：`spoolkit.channels` 或 `SPOOLKIT_CHANNEL_PLUGINS`。内置名优先。测试：`tests/bridge/test_channels.py`。
