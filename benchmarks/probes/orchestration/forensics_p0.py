"""P0 取证：逐条还原 36 条真机读数（只读，不改任何读数/夹具）。

回答五件事：
1. 撞步数上限前它在做什么（工具序列的形状：重复 / 大文件 / 搜索失败 / 反复写同一文件）；
2. 素材够不够（撞上限时它已经拿到多少东西、有没有可用结论）；
3. 上下文重置（resets）在什么条件下发生、N 次对应什么；
4. `final` 是系统判词还是模型结论（逐条比对 final 的前缀与模型最后一个回合）；
5. 我们自己的机制里有哪些在助推不收敛（预算放大 / 重试 / 督导 / 护栏）。

跑法：
    .venv\\Scripts\\python.exe benchmarks\\probes\\orchestration\\forensics_p0.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

P = json.loads((HERE / "real-readings.json").read_text(encoding="utf-8"))
R = P["readings"]

#: 系统判词的字面前缀（`loop.py` 里写死的那两句 + 空回合那句）。
VERDICT_PREFIXES = (
    "已达步数上限，任务未完成",
    "任务没做完，收手的原因",
    "模型在提出改动后停止产出可执行的回合",
)

#: 只读/查看类工具（用来数「撞上限前它在看还是在写」）。
READ_TOOLS = {
    "read_file", "list_dir", "search_code", "survey", "dir_stats", "tool_help",
    "read_diagnosis", "verify_file", "dispatch",
}
WRITE_TOOLS = {"write_file", "replace_lines", "replace_text", "delete_file"}


def kind_of(run: dict) -> str:
    """这一条是被谁终止的（从现有读数能看出来的口径）。"""
    final = run["final"] or ""
    if run["finished"]:
        return "模型完成"
    if any(final.startswith(one) for one in VERDICT_PREFIXES):
        return "步数上限"
    return "其它"


def tail_shape(names: list[str], n: int = 6) -> str:
    tail = names[-n:]
    return "→".join(tail)


def repeat_run(names: list[str]) -> tuple[int, int]:
    """最长连续同一工具名、总调用数（粗口径）。"""
    best = streak = 1 if names else 0
    for before, now in zip(names, names[1:]):
        streak = streak + 1 if now == before else 1
        best = max(best, streak)
    return best, len(names)


def main() -> int:
    print(f"读数 {len(R)} 条；HEAD {P['scene']['head'][:12]}；"
          f"工具 {P['scene']['tool_count']} 件；system {P['scene']['system_tokens']} token")
    print(f"每格重复 {P['repeats']} 次；整批 {P['elapsed_seconds']}s 预算 {P['budget_seconds']}s")
    print()

    print("=" * 78)
    print("① 终止方式分布（口径：finished / final 前缀）")
    buckets = Counter(kind_of(one) for one in R)
    for key, count in buckets.most_common():
        print(f"   {key:8s} {count:2d}/{len(R)}")
    print()

    print("=" * 78)
    print("② 撞上限的运行：步数 / 记录上限 / 实际放大 / 上下文重置")
    print(f"{'标签':26s} {'步':>3s} {'max_steps':>9s} {'放大':>4s} {'reset':>5s} "
          f"{'工具':>4s} {'输入tok':>8s} {'可用':>4s}")
    grown = 0
    reset_hist = Counter()
    for one in sorted(R, key=lambda x: x["label"]):
        if kind_of(one) != "步数上限":
            continue
        boosted = one["steps"] > one["max_steps"]
        grown += 1 if boosted else 0
        reset_hist[one["resets"]] += 1
        checks = {k: v for k, v in one["checks"].items() if k != "_hits"}
        passed = sum(1 for v in checks.values() if v)
        print(f"{one['label']:26s} {one['steps']:3d} {one['max_steps']:9d} "
              f"{'是' if boosted else '否':>4s} {one['resets']:5d} "
              f"{one['tool_calls']:4d} {one['prompt_tokens']:8d} {passed:2d}/{len(checks)}")
    print(f"   → 撞上限 {buckets.get('步数上限', 0)} 条里，实际步数超过记录上限的：{grown} 条")
    print(f"   → 重置次数分布（撞上限那批）：{dict(sorted(reset_hist.items()))}")
    all_reset = Counter(one["resets"] for one in R)
    print(f"   → 重置次数分布（全部 36 条）：{dict(sorted(all_reset.items()))}")
    print(f"   → 至少重置过一次：{sum(1 for one in R if one['resets'] > 0)}/{len(R)}；"
          f"重置总次数 {sum(one['resets'] for one in R)}")
    print()

    print("=" * 78)
    print("③ 撞上限前它在做什么：工具序列尾段 + 形状统计")
    shapes = Counter()
    for one in sorted(R, key=lambda x: x["label"]):
        if kind_of(one) != "步数上限":
            continue
        names = one["tool_names"]
        best, total = repeat_run(names)
        reads = sum(1 for n in names if n in READ_TOOLS)
        writes = sum(1 for n in names if n in WRITE_TOOLS)
        same_file_writes = 0
        # 反复写同一文件：从事件流里拿不到路径，这里按「写工具连续出现」近似。
        for before, now in zip(names, names[1:]):
            if before in WRITE_TOOLS and now in WRITE_TOOLS:
                same_file_writes += 1
        shapes[(reads, writes, best)] += 1
        print(f"{one['label']:26s} 读{reads:2d} 写{writes:2d} 最长连续同名{best:2d} "
              f"尾段 {tail_shape(names)}")
        print(f"{'':26s} 工具序列 {'→'.join(names)}")
    print()

    print("=" * 78)
    print("④ 素材够不够：撞上限时最后一次工具结果之后它做了什么 / 有没有写结论")
    enough_markers = 0
    for one in sorted(R, key=lambda x: x["label"]):
        if kind_of(one) != "步数上限":
            continue
        checks = {k: v for k, v in one["checks"].items() if k != "_hits"}
        hits = one["checks"].get("_hits", [])
        # 「素材够」的机械代理：已经读过 ≥6 个工具结果，且最后一次调用是查看类。
        names = one["tool_names"]
        last_is_read = bool(names) and names[-1] in READ_TOOLS
        if len(names) >= 6 and last_is_read:
            enough_markers += 1
        print(f"{one['label']:26s} 工具调用 {len(names):2d} 末次{'查看' if last_is_read else '写/其它'} "
              f"覆盖命中 {len(hits) if isinstance(hits, list) else '-'}/6 判据 {checks}")
    print(f"   → 「看过 ≥6 次且最后一次仍在查看」的运行：{enough_markers} 条")
    print()

    print("=" * 78)
    print("⑤ final 是判词还是结论：逐条原文")
    for one in sorted(R, key=lambda x: x["label"]):
        final = (one["final"] or "").strip()
        system_verdict = any(final.startswith(one2) for one2 in VERDICT_PREFIXES)
        turns = one["model_turns"]
        last_turn = turns[-1] if turns else {}
        print(f"{one['label']:26s} 判词={system_verdict!s:5s} "
              f"末回合 done={last_turn.get('done')} 调用数={last_turn.get('tool_calls')} "
              f"final长度={len(final)}")
        print(f"{'':26s} {final[:200]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
