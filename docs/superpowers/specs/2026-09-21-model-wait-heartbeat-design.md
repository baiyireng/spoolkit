# 模型等待心跳 —— 设计文档

日期：2026-09-21
状态：待评审

## 1. 为什么做这一件事

llama.cpp 走 `stream: false`：整段生成完之前 HTTP **一个字节都不回**。
终端、网页、QQ 在此期间都没有新行。本机 Qwen IQ2_S 一次工具调用可以默默
等五分钟——人以为挂死了，其实还在现有超时公式里
（`max(180, max_tokens × 0.2)` 秒）。

缺口是**「还在等」的信号**，不是超时太长。

## 2. 范围

做：

- 任意一次 `gateway.chat()` 等待满 15 秒后开始报心跳，之后每 15 秒一次。
- CLI 散文模式写 stderr；`--events` 发 `wait` JSON。网页用同一条事件。
- llama.cpp 心跳带上这次请求的上限（现有 `_timeout_for`）。

不做：

- 不改超时公式，不因「空闲」中断请求。
- 不接 SSE / 不把 token 打到屏幕（流式是再下一刀）。
- 不把间隔放进 `limits.json`（给人看的，不是能力标定）。
- 不给 QQ 手机每 15 秒推一条。
- 不占用网页红色探活横幅（那是模型/隧道连不上）。

## 3. 核心决策

### 3.1 心跳包在网关上，不包在主循环里

挂死发生在任何一次模型调用：主循环、拆解、督导、派发、研究会话。
只改 `loop.py` 会漏掉 `plan.py` 那些直接 `gateway.chat` 的路径。

在 `CountingGateway` 旁边加 `WaitingGateway`：只拦 `chat()`，其余属性透传。
`provider_gateway` 返回前包上它，所有执行路径自动带心跳。

假模型秒回则零心跳，现有回放测试不用改。

### 3.2 同一进程只走一条输出路

`--events` 时 stdout 只能有 JSON 行。子进程若同时写 stderr，hub 终端会
再看到一截散文，网页已经从 JSON 画过一遍。

| 模式 | 心跳去哪 |
|---|---|
| `spool run`（散文） | stderr |
| `spool run --events`（网页 / QQ / MCP 子进程） | stdout 一行 `wait` |

监听器在 `run` / `plan` / `chat` 拿到 gateway 之后立刻绑：散文绑 stderr，
`--events` 绑 `EventWriter.emit`。漏绑的路径保持静默（等于今天），不崩溃。
回调签名 `on_wait(elapsed: int, limit: float | None)`：`elapsed` 是整秒；
没有上限时 `limit` 为 `None`，JSON 里省略该键。

### 3.3 新事件 `wait`，必须进协议白名单

`parse_line` 对未知 `type` 返回 `None`。不加白名单，网页永远看不到心跳。

```json
{"type":"wait","elapsed":15,"limit":491}
```

- `elapsed`：从这次 `chat()` 开始的整秒，必有。
- `limit`：这次请求最多还等多久（秒）。llama.cpp 用 `_timeout_for(max_tokens)`；
  其它供应商没有就**省略这个键**，不要填 0。

网页：在事件流里加一行灰字
「模型生成中，已等待 15s / 上限 491s」（没有 `limit` 就只写已等待）。
不改 `#health`。

QQ / MCP：当普通事件留在日志。桥只看 `finished` / `awaiting`，不会推到手机。
网页 runner 吸收 `wait` 时 **不得** 改 `_awaiting`。

### 3.4 间隔写死 15 秒

第一次心跳在等待满 15 秒时；15 秒内返回则完全不说话。
之后每 15 秒一次，直到 `chat()` 返回（成功或抛错）。

## 4. 装配与出错

```
provider_gateway
  → WaitingGateway(inner=LlamaCppGateway|GeminiGateway|FakeModel)
       可选再被 CountingGateway 包一层（计数仍只在 chat 返回后加）
```

`WaitingGateway.chat`：

1. 记下开始时间与本次 `limit`（inner 有 `_timeout_for` 才算）。
2. 起守护线程：循环 `sleep(15)`，若仍在等则调 `on_wait(int(elapsed), limit)`。
3. 调 `inner.chat`。
4. `finally`：设停止标志并 `join`。返回之后不允许再跳一次。

约束：

- inner 抛 `LlamaCppError` / `ReadTimeout` 包装后的错误，心跳必须停。
- `on_wait` 抛错只吞掉，不能把正在等的那次请求打崩。
- 心跳不是失败：不改错误类型、不叫督导、不当成空转。

时钟与睡眠可注入，测试不真睡 15 秒。

## 5. 测试

不打真 llama-server。

- 假 inner 堵住、时钟按 15 秒推进 → 心跳次数等于堵住的拍数。
- 假 inner 立刻返回 → 零次心跳。
- 假 inner 抛错 → 线程停，心跳停在抛错前。
- `parse_line` 认 `wait`；没有 `limit` 的也认。
- 网页 `Runner._absorb(wait)` 之后 `awaiting` 仍为 0。
- 假模型回放全套保持绿。

## 6. 文档

上手故障表「终端几分钟没有新行」改为：满 15 秒会报还在等；
仍等到现有上限。假模型缺脚本会立刻退出，不会替你写代码。

README 已知限制里 llama.cpp 那条同步改一句。

## 7. 明确不做

- HTTP `stream: true`、把增量 token 送给循环或屏幕。
- 墙钟空闲超时、提前杀掉慢生成。
- 改 `SECONDS_PER_OUTPUT_TOKEN` 或 `_timeout_for`。
- 心跳进 `limits.KNOBS`。
- T8 本地切块、多模态、weft、浏览器级验证。
