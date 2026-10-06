"""对照读数的小检视器（报告用，不是判据）。

    python inspect_readings.py            # 逐次：步数/调用序列/是否完成
    python inspect_readings.py plans      # 贴出每一次写出来的计划（成功与失败）
    python inspect_readings.py final A    # 贴出 A 任务每次的最终答复
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAYLOAD = json.loads((HERE / "real-readings.json").read_text(encoding="utf-8"))


def rows():
    return PAYLOAD["readings"]


def mode_default() -> None:
    print(f"HEAD {PAYLOAD['scene']['head'][:12]} 工具 {PAYLOAD['scene']['tool_count']} "
          f"system {PAYLOAD['scene']['system_tokens']} token")
    print(f"沙箱 : {PAYLOAD['sandbox']['project']}")
    for one in rows():
        names = ",".join(one["tool_names"])
        checks = {k: v for k, v in one["checks"].items() if k != "_hits"}
        print(
            f"{one['label']:28s} 步 {one['steps']:2d}/{PAYLOAD['repeats']} "
            f"调用 {one['tool_calls']:2d} 编排 {one['orchestrate_calls']} "
            f"子调用 {one['subcalls']} 完成 {one['finished']} "
            f"墙钟 {one['wall_seconds']:6.1f}s 输入 {one['prompt_tokens']:6d} "
            f"可用 {sum(1 for v in checks.values() if v)}/{len(checks)}"
        )
        print(f"  工具序列: {names}")
        if one["orchestrate_rejects"]:
            print(f"  拒绝: {one['orchestrate_rejects'][0][:300]}")
    print(f"\n跳过/未完成: {json.dumps(PAYLOAD['skipped'], ensure_ascii=False)}")


def mode_tsv() -> None:
    """逐次读数一行一条（贴进报告/表格用）。"""
    cols = ("head", "started_at", "label", "arm", "temperature", "steps", "model_calls",
            "prompt_tokens", "completion_tokens", "wall_seconds", "tool_calls",
            "model_multi_call_turns", "model_max_calls_in_turn", "orchestrate_calls",
            "subcalls", "orchestrate_plans_ok", "orchestrate_plans_rejected", "finished",
            "tool_count", "system_tokens", "resets")
    print("\t".join(cols))
    for one in rows():
        print("\t".join(str(one.get(key, "")) for key in cols))
    print("\n每跑一次记下的 (版本, 工具件数, system token):")
    for item in sorted({(one["head"], one["tool_count"], one["system_tokens"]) for one in rows()}):
        print("   ", item)


def mode_plans() -> None:
    found = 0
    for one in rows():
        for index, text in enumerate(one["orchestrate_plan_texts"]):
            found += 1
            shape = one["orchestrate_plan_shape"][index]
            print(f"\n--- {one['label']} 第 {index + 1} 份（合法 {shape['ok']}）---")
            print(text[:2000])
            if not shape["ok"]:
                print(f"  [拒绝] {one['orchestrate_full_rejects'][index][:600]}")
    print(f"\n一共 {found} 份计划")


def mode_final(task: str) -> None:
    for one in rows():
        if one["task"] != task:
            continue
        print(f"\n===== {one['label']}（完成 {one['finished']}）=====")
        print((one["final"] or "")[:2500])


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "default"
    if what == "plans":
        mode_plans()
    elif what == "final":
        mode_final(sys.argv[2] if len(sys.argv) > 2 else "A-todo")
    elif what == "tsv":
        mode_tsv()
    else:
        mode_default()
