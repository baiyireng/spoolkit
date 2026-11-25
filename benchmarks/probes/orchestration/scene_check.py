"""核对装配现场在整批跑里有没有变（报告用）。"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
P = json.loads((HERE / "real-readings.json").read_text(encoding="utf-8"))

print("scene(开头):", {k: P["scene"][k] for k in
                     ("head", "tool_count", "has_orchestrate", "system_tokens", "system_chars")})
print("scene(结尾):", P.get("scene_after"))
print("skipped    :", P["skipped"])
print("每跑一次记下的 (工具件数, system token, max_steps):")
for one in sorted({(r["tool_count"], r["system_tokens"], r["max_steps"]) for r in P["readings"]}):
    print("   ", one)
print("off 臂工具件数:", sorted({r["tool_count"] for r in P["readings"] if r["arm"] == "off"}))
print("on  臂工具件数:", sorted({r["tool_count"] for r in P["readings"] if r["arm"] == "on"}))
print("完成的运行   :", [(r["label"], r["steps"]) for r in P["readings"] if r["finished"]])
print("上下文重置次数:", sorted({r["resets"] for r in P["readings"]}))
print("tap_ok       :", sorted({r["tap_ok"] for r in P["readings"]}))
print("orchestrate 调用总数:", sum(r["orchestrate_calls"] for r in P["readings"]))
print("子调用总数:", sum(r["subcalls"] for r in P["readings"]))
print("模型自己一步最多几个调用:", max(r["model_max_calls_in_turn"] for r in P["readings"]))
print("多调回合总数:", sum(r["model_multi_call_turns"] for r in P["readings"]))
print("停止原因（trace 里的收尾行）:")
for one in P["readings"]:
    tail = [line for line in one.get("trace", []) if "收手" in line or "上限" in line or "重置" in line]
    if tail:
        print(f"   {one['label']}: {tail[-1][:120]}")
print("A/on/t0.0 的三条判据:", [
    {k: v for k, v in r["checks"].items() if k != "_hits"}
    for r in P["readings"] if r["task"] == "A-todo" and r["arm"] == "on" and r["temperature"] == 0.0
])
