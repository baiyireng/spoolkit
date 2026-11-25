"""真机探针读数的小检视器（报告用，不是判据）。

    python inspect_readings.py            # 逐次：步数/调用序列/是否完成
    python inspect_readings.py tsv        # 一行一条（贴表格用）
    python inspect_readings.py final A    # 贴出 A 任务每次的最终答复

读数来自 `real_probe.py` 的产物 `real-probe-readings.json`（**跑过才有**）。
当年三臂那批归档 `real-readings.json`（S10 删掉 `orchestrate` 前的证据）字段名
不一样，也已冻结不再更新，所以这个检视器不再读它。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
READINGS = HERE / "real-probe-readings.json"

if not READINGS.is_file():
    raise SystemExit(f"还没有读数：{READINGS.name}（先跑 real_probe.py）")

PAYLOAD = json.loads(READINGS.read_text(encoding="utf-8"))


def rows() -> list[dict]:
    return PAYLOAD["readings"]


def mode_default() -> None:
    print(f"HEAD {PAYLOAD['scene']['head'][:12]} 工具 {PAYLOAD['scene']['tool_count']} "
          f"system {PAYLOAD['scene']['system_tokens']} token")
    print(f"沙箱 : {PAYLOAD['sandbox']['project']}")
    for one in rows():
        names = ",".join(one["tool_names"])
        checks = {k: v for k, v in one["checks"].items() if not k.startswith("_")}
        print(
            f"{one['label']:24s} 步 {one['steps']:2d}/{one['max_steps']} "
            f"调用 {one['tool_calls']:2d} 完成 {one['finished']} "
            f"墙钟 {one['wall_seconds']:6.1f}s 输入 {one['prompt_tokens']:6d} "
            f"可用 {sum(1 for v in checks.values() if v)}/{len(checks)} "
            f"压缩 {one['compactions']} 收束拦写 {one['write_blocked']} "
            f"重跑 {one['repeat_executions']} 拦下 {one['refused_useless']}/{one['refused_undecided']}"
        )
        print(f"  工具序列: {names}")
        if one["refused_useless_tools"] or one["refused_undecided_tools"]:
            print(f"  拦下的工具: 无用重复 {one['refused_useless_tools']} "
                  f"判不出 {one['refused_undecided_tools']}")
    print(f"\n跳过/未完成: {json.dumps(PAYLOAD['skipped'], ensure_ascii=False)}")


def mode_tsv() -> None:
    """逐次读数一行一条（贴进报告/表格用）。"""
    cols = ("head", "started_at", "label", "task", "temperature", "steps", "model_calls",
            "prompt_tokens", "completion_tokens", "wall_seconds", "tool_calls",
            "model_multi_call_turns", "model_max_calls_in_turn", "compactions", "resets",
            "judgement_lines_kept", "write_allowed", "write_blocked", "repeat_executions",
            "refused_useless", "refused_undecided", "command_runs", "finished",
            "tool_count", "system_tokens")
    print("\t".join(cols))
    for one in rows():
        print("\t".join(str(one.get(key, "")) for key in cols))
    print("\n每跑一次记下的 (版本, 工具件数, system token):")
    for item in sorted({(one["head"], one["tool_count"], one["system_tokens"]) for one in rows()}):
        print("   ", item)


def mode_final(task: str) -> None:
    for one in rows():
        if one["task"] != task:
            continue
        print(f"\n===== {one['label']}（完成 {one['finished']}）=====")
        print((one["final"] or "")[:2500])


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "default"
    if what == "final":
        mode_final(sys.argv[2] if len(sys.argv) > 2 else "A-todo")
    elif what == "tsv":
        mode_tsv()
    else:
        mode_default()
