# Agent 主循环流程

权威控制流图。锚点：`agent/loop.py`（`AgentLoop.run`）、`cli/commands/run.py`、`cli/settle.py`、`agents/supervisor.py`。  
改控制流 → 同步本文件 + 变更记录（`.cursor/rules/agent-loop-flow.mdc`）。

## 总览

```mermaid
flowchart TB
  subgraph 入口["① 入口（CLI / 探针）"]
    U[用户 goal] --> RUN["spool run / probe"]
    RUN --> ASM["装配 AgentLoop<br/>网关 · 工具表 · Config · pending"]
    ASM --> HIST["history = 首条 user 消息<br/>goal 或续跑提示"]
  end

  subgraph 步进["② 主循环 while True（每步）"]
    HIST --> PRE{"预算/打转？"}
    PRE -->|步数到顶或打转信号| SUP["督导 supervise<br/>续期 / 改道 / 收手 / 交还写 final"]
    SUP -->|续期·改道| FEED[注入 feedback / redirect]
    SUP -->|收手| FAILEND[未完成出口]
    PRE -->|正常| FEED2[组装本步提示<br/>收束 CONVERGE / 强制结论 / 教训…]
    FEED --> FEED2
    FEED2 --> LLM["调用模型"]
    LLM -->|网络/模型炸| FAILEND
    LLM --> PARSE["parse_turn"]

    PARSE -->|工具调用| GUARD["护栏<br/>重复拦 · 观察扣留 · 收束拦探索"]
    GUARD -->|放行| TOOL["执行工具<br/>读/写/命令/派发…"]
    TOOL --> BOOK["记账<br/>changed_paths · pending · 进度"]
    BOOK --> REV["落盘后机械审查可选"]
    REV --> STEP["step++ / 被全拦可退还预算"]
    GUARD -->|硬拦| STEP

    PARSE -->|done=true + final| BOUNCE{"_bounce_unwritten<br/>假完成 / 谎称完成 / 暂存未确认"}
    BOUNCE -->|退回| FEED
    BOUNCE -->|通过| GATE["完成门控 gate<br/>审查发现 → 有限轮自动修"]
    GATE -->|再修| FEED
    GATE -->|过| OKEND["成功出口 LoopResult"]

    PARSE -->|收束态纯文本无工具| OKEND
    STEP --> PRE
  end

  subgraph 收尾["③ 循环外收尾"]
    OKEND --> SETTLE["settle<br/>待确认清单 → 用户确认 / auto 落盘"]
    FAILEND --> SETTLE2["仍可 settle 已落盘部分<br/>+ 最终状态/验证附言"]
    SETTLE --> OUT["事件流 + 报告<br/>finished / final / model_final"]
    SETTLE2 --> OUT
  end
```

## 分层

| 层 | 职责 | 代码 |
|----|------|------|
| ① 入口 | goal、装配网关/工具/Config | `run.py` → `assemble_loop` |
| ② 步进 | 一步一次模型调用；督导仅预算尽/打转 | `AgentLoop.run` |
| ③ 收尾 | settle 待确认 → 工作区；`LoopResult` | `settle.py` |

| 分支 | 行为 |
|------|------|
| 督导 | 续期 / 改道 / 收手 / 交还写 final |
| 护栏 | 重复·观察扣留·收束拦探索；可退还步数。放行「有理由的重取」时**按成因分账**（结果被压掉 / 对象真变了 / 待确认落了盘 / 失败重试），总额度不放宽（`_REFRESH_LIMIT`），「压掉」那本份额更小（`_REFRESH_COMPACTED_LIMIT`） |
| `_bounce_unwritten` | 假完成 / 谎称完成 / 暂存未确认；默认不拦「只读 done」 |
| gate | 高严重度 → 有限轮自动修 |
| 收束 | 禁探索、限写；可收下纯文本结论 |

## 同步

须改图：入口/`Config` 控制流、`while` 分支、settle/`LoopResult` 语义。  
不改图：话术、标定数值、单测、无关记账。

## 变更记录

| 日期 | 变更 | 原因 |
|------|------|------|
| 2026-10-12 | 初版总览图 | 跟踪 agent 流程 |
| 2026-10-12 | 文风收紧 | 与活文档统一为契约表风格 |
| 2026-10-16 | 护栏放行按成因分账（D6） | 一个成因把整份额度吃光，真正有新内容的那次重取被硬拦；总额度不放宽，只留一次给"重取能拿到新内容" |
