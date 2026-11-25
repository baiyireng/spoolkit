"""把网关每一个回合的**请求与应答原话**留一份（只读诊断，不改行为）。

为什么需要它：只看事件流分不清「收束指令发下去了但模型不理」与「收束指令根本
没送到」——事件流里 `tool` 的 detail 被截到 200 字，而且不含模型那一轮的
JSON 正文。这里把两侧都留下来：

- `sent`：这一轮**发给模型**的全文里有没有收束指令、有没有强制收敛那两句；
- `reply`：模型这一轮的原话（JSON 正文，逐字）。

用法（`p0_converge_probe.py` 里）：

    $env:ORCH_DUMP_TURNS = "benchmarks/probes/orchestration/p0-turns.json"
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from spoolkit.context import templates as T


def tap_gateway(gateway, sink: list[dict]) -> None:
    """把 `gateway.chat` 包一层：每一轮把两侧都追加进 `sink` 并落盘。

    落盘时机是**每一轮之后**：这样即使整批被预算切断，已经拿到的回合还在。
    """
    original = gateway.chat
    path = Path(os.environ["ORCH_DUMP_TURNS"])

    def tap(request):
        body = "\n".join(message.content for message in request.messages)
        response = original(request)
        sink.append(
            {
                "n": len(sink) + 1,
                "sent_chars": len(body),
                "converge_injected": T.CONVERGE_NOW in body,
                "force_commit": T.FORCE_COMMIT in body,
                "force_conclude": T.FORCE_CONCLUDE in body,
                "reply": response.text,
            }
        )
        path.write_text(
            json.dumps(sink, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return response

    gateway.chat = tap


def main() -> int:
    """单独跑一次「收束那一步」的诊断（不走沙箱，只用假模型）。"""
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("这个模块由 p0_converge_probe.py 通过 ORCH_DUMP_TURNS 使用，不单独运行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
