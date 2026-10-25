"""把真模型对照的读数渲染成一张能贴的对照表（报告用，判据仍在本脚本之外）。

    python render_table.py            # 全部设置
    python render_table.py plans      # 附上模型写出来的计划（成功/失败各一份）
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAYLOAD = json.loads((HERE / "real-readings.json").read_text(encoding="utf-8"))


def cell(values: list[float]) -> str:
    if not values:
        return "—"
    median = statistics.median(values)
    if len(set(values)) == 1:
        return f"{median:g}"
    return f"{median:g}（{min(values):g}–{max(values):g}）"


def main() -> int:
    scene = PAYLOAD["scene"]
    after = PAYLOAD.get("scene_after") or {}
    print(f"HEAD {scene['head'][:12]}｜工具 {scene['tool_count']} 件（orchestrate 在册："
          f"{scene['has_orchestrate']}）｜system {scene['system_tokens']} token")
    if after.get("head") and after["head"][:12] != scene["head"][:12]:
        print(f"⚠ 跑的过程中 HEAD 变了：{after['head'][:12]}")
    if after.get("src_dirty"):
        print("⚠ 跑的过程中 src/ 有未提交改动：")
        print(after["src_dirty"])
    print(f"生成 {PAYLOAD['generated_at']}；预算 {PAYLOAD['budget_seconds']}s；"
          f"用了 {PAYLOAD['elapsed_seconds']}s；每格 {PAYLOAD['repeats']} 次")
    print(f"沙箱 {PAYLOAD['sandbox']['project']}（失败测试 exit={PAYLOAD['sandbox']['fail_exit']}）")
    print()
    keys = (
        ("steps", "步数"),
        ("model_calls", "模型调用"),
        ("prompt_tokens", "输入 token"),
        ("completion_tokens", "输出 token"),
        ("wall_seconds", "墙钟 s"),
        ("tool_calls", "工具调用"),
        ("model_multi_call_turns", "一步多调回合"),
        ("orchestrate_calls", "orchestrate 次数"),
        ("subcalls", "子调用数"),
    )
    tasks = [one for one in PAYLOAD["goals"]
             if any(r["task"] == one for r in PAYLOAD["readings"])]
    for task in tasks:
        for temperature in PAYLOAD["settings"]:
            arms = [one for one in ("off", "on", "control")
                    if PAYLOAD["summary"].get(f"{task}/t{temperature}/{one}")]
            if not arms:
                continue
            rows = {arm: PAYLOAD["summary"][f"{task}/t{temperature}/{arm}"] for arm in arms}
            title = {"off": "off（一次一调）", "on": "on（一步多调）",
                     "control": "control（同尺寸安慰剂工具）"}
            print(f"### {task} 温度 {temperature}")
            print("| 指标 | " + " | ".join(title[one] for one in arms) + " |")
            print("| --- |" + " --- |" * len(arms))
            for key, name in keys:
                cells = []
                for arm in arms:
                    cells.append(
                        cell([one[key] for one in PAYLOAD["readings"]
                              if one["task"] == task and one["arm"] == arm
                              and one["temperature"] == temperature])
                    )
                print(f"| {name} | " + " | ".join(cells) + " |")
            names = list(rows[arms[0]].get("checks", {}))
            for name in names:
                cells = []
                for arm in arms:
                    ok = rows[arm]["checks"].get(name, {}).get("pass", 0)
                    total = rows[arm]["checks"].get(name, {}).get("of", 0)
                    cells.append(f"{'✅' if ok == total and total else '❌'} {ok}/{total}")
                print(f"| 可用性：{name} | " + " | ".join(cells) + " |")
            for arm in arms:
                row = rows[arm]
                print(
                    f"- {arm}: 完成 {row['finished']}/{row['runs']}；"
                    f"用编排的轮次 {row['orchestrate_used_runs']}/{row['runs']}；"
                    f"合法计划 {row['orchestrate_plans_ok']}，被拒 {row['orchestrate_plans_rejected']}"
                )
            print()
    if PAYLOAD["skipped"]:
        print("未完成/跳过（如实记）：")
        for one in PAYLOAD["skipped"]:
            print(f"- {one['label']}: {one['reason']}")
        print()

    if len(sys.argv) > 1 and sys.argv[1] == "plans":
        ok_shown = 0
        bad_shown = 0
        for one in PAYLOAD["readings"]:
            for index, text in enumerate(one["orchestrate_plan_texts"]):
                shape = one["orchestrate_plan_shape"][index]
                if shape["ok"] and ok_shown < 1:
                    ok_shown += 1
                    print(f"\n**一份合法计划**（{one['label']}）：\n```json\n{text[:1800]}\n```")
                if not shape["ok"] and bad_shown < 1:
                    bad_shown += 1
                    print(f"\n**一份被拒的计划**（{one['label']}）：\n```json\n{text[:1800]}\n```")
                    print("拒绝原文：")
                    print("```\n" + one["orchestrate_full_rejects"][index][:1200] + "\n```")
        if not ok_shown and not bad_shown:
            print("\n没有任何一份 `orchestrate` 计划（模型一次都没调它）。")
        if not ok_shown:
            print("\n没有合法计划。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
