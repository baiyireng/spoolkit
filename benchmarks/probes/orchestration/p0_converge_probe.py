"""P0 真机小样本：**收束机制**在 8080 上跑两个真形状任务。

它与 `real_probe.py`（同目录那份单路真机探针）的关系：

- **同一个沙箱配方、同一段用户原话、同一套机械判据**（`GOALS` / `checks_for`
  都从 `real_probe` import，一个字不改——判据先定好再跑）；
- **自变量是收束期那几条修法**（按目标行数放大预算 / 临近预算收束 / 收束期写额度）
  的开与关，写在下面的 `ARMS` 里；
- 上一轮那批三臂对照（`real-readings.json`，36 跑）是**历史证据**：它证明
  `orchestrate` 36 次一次没被调用，S10 据此把那件工具整层删掉（见 `README-real.md`
  的 S10 一节）。本轮不再有臂可跑，口径是**同一份判据下各机制臂的前后对照**——
  这一点必须写在报告里，否则读数会被读成「同一件事的几次采样」。

三条要求落在这里：

1. `--total-budget` 有界：撞线时**先减重复次数，不减任务**；
2. 每一次都记 `HEAD + PROMPT_VERSION + 工具件数 + system token`（提示词刚改过，
   读数只能在标注版本上解释）；
3. 可用性判据读**模型自己写的结论**（`concluded` / `model_final`），不读那段
   混着系统判词的报告正文——否则「系统判词里有分类两个字」也会被判绿。

用法（不重启 8080；代理先清干净）：

    $env:ORCH_MAX_STEPS = "8"
    $env:ORCH_TOTAL_BUDGET = "4200"
    .venv\\Scripts\\python.exe benchmarks\\probes\\orchestration\\p0_converge_probe.py scenes
    .venv\\Scripts\\python.exe benchmarks\\probes\\orchestration\\p0_converge_probe.py run
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(HERE))

try:  # pragma: no cover - 只影响终端那条路
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

STATE_FILE = HERE / ".p0-state.json"
OUT = HERE / "p0-converge-readings.json"
LOG_PATH = HERE / "p0-converge-log.txt"

#: 沙箱配方与用户原话**从上一轮那份脚本 import**：不抄第二份，也不可能改到
#: 判据（判据夹具在 `seeds/expected/checks/probes.json`，这里一个字不碰）。
from real_probe import (  # noqa: E402
    GOALS,
    checks_for,
    guard_readings,
    instrument_guards,
    _build_sandbox,
    _isolate_local_http,
    _system_text,
    reset_sandbox,
)

#: 机制臂。名字写在读数里，报告按它分组。
#:
#: - `base`：**不放大、不收束**（硬上限 0 / 收束关）。它等价于「只留下修①，
#:   把修③关掉」，用来量收束本身值多少；
#: - `converge`：修① + 修③（硬上限 0、剩余 ≤ `converge_at_steps` 步收束），
#:   写成果**不设上限**（`converge_write_grace = -1`，旧行为）；
#: - `converge-nowrite` / `converge-onewrite`：在 `converge` 之上只动
#:   `converge_write_grace`（0 = 收束期只准给结论，1 = 只准写一次）。
#:   这是 O6/O8 的对照实验：同一份提示、同一份沙箱，唯一差别是**收束期还能不能
#:   写**。读数看 `write_allowed` / `write_blocked` 与 `checks` 三列一起比。
#:
#: `converge` 臂把 `-1` **显式写出来**，不靠默认值：这一档的默认值已经改成 0
#: （见 `limits.converge_write_grace` 的依据），若这里省略，两臂会变成同一个
#: 配置、对照实验静默失效——`tests/test_real_probe_targets.py` 钉着这一点。
ARMS: dict[str, dict[str, float]] = {
    "base": {"step_budget_hard_cap": 0, "converge_at_steps": 0},
    "converge": {
        "step_budget_hard_cap": 0,
        "converge_at_steps": 3,
        "converge_write_grace": -1,
    },
    "converge-nowrite": {
        "step_budget_hard_cap": 0,
        "converge_at_steps": 3,
        "converge_write_grace": 0,
    },
    "converge-onewrite": {
        "step_budget_hard_cap": 0,
        "converge_at_steps": 3,
        "converge_write_grace": 1,
    },
}


def say(text: str) -> None:
    print(text)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(text + "\n")


def _settings() -> list[str]:
    raw = os.environ.get("ORCH_P0_ARMS", "base,converge")
    return [one.strip() for one in raw.split(",") if one.strip() in ARMS]


def _tasks() -> list[str]:
    raw = os.environ.get("ORCH_TASKS", "")
    if not raw.strip():
        return list(GOALS)
    return [one.strip() for one in raw.split(",") if one.strip() in GOALS]


def _repeats() -> int:
    return int(os.environ.get("ORCH_REPEATS", "2"))


def _budget() -> int:
    return int(os.environ.get("ORCH_TOTAL_BUDGET", "4200"))


def _max_steps() -> int:
    return int(os.environ.get("ORCH_MAX_STEPS", "8"))


# ---------------------------------------------------------------------------
# 阶段 0：沙箱（与上一轮同一份配方，逐字）
# ---------------------------------------------------------------------------


def stage_sandbox() -> int:
    from spoolkit import sandbox_dirs

    base = Path(sandbox_dirs.make_accessible_temp_dir("spool-p0-converge-"))
    project = base / "project"
    note = _build_sandbox(project)
    python = REPO / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        python = Path(sys.executable)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    finished = subprocess.run(
        [
            str(python), "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "tests/services/test_service_ledger.py",
        ],
        cwd=str(project), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300,
    )
    tail = "\n".join((finished.stdout or "").strip().splitlines()[-6:])
    STATE_FILE.write_text(
        json.dumps(
            {
                "project": str(project),
                "todo_seed": 6,
                "fail_exit": finished.returncode,
                "fail_tail": tail,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    say(f"沙箱 : {project}")
    say(f"配方 : {note}")
    say(f"失败测试：exit={finished.returncode}")
    say(tail)
    return 0


# ---------------------------------------------------------------------------
# 场景：HEAD + PROMPT_VERSION + 工具件数 + system token
# ---------------------------------------------------------------------------


def scene_now() -> dict:
    from spoolkit.config import Config
    from spoolkit.llm.tokenizer import OfflineTokenCounter
    from spoolkit.cli.runtime import assemble_loop

    from real_probe import _NullGateway, _scene_wiring

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(REPO), capture_output=True, text=True
    ).stdout.strip()
    loop = assemble_loop(
        REPO,
        _NullGateway(),
        config=Config(project_root=REPO, supervise=False, context_window=8192),
        wiring=_scene_wiring(REPO),
    )
    system_text = _system_text(loop)
    counter = OfflineTokenCounter()
    from spoolkit.context import templates as T

    return {
        "head": head,
        "prompt_version": T.PROMPT_VERSION,
        "tool_count": len(loop.registry.names()),
        "system_chars": len(system_text),
        "system_tokens": counter.count(system_text),
        "effective_tokens": loop._budget.effective(),
        "tools": list(loop.registry.names()),
    }


# ---------------------------------------------------------------------------
# 阶段 1：跑
# ---------------------------------------------------------------------------


def run_once(task: str, arm: str, repeat: int, label: str) -> dict:
    from spoolkit.cli.runtime import LoopWiring, assemble_loop, counter_for
    from spoolkit.config import Config
    from spoolkit.llm.llamacpp import LlamaCppGateway
    from spoolkit.tools.edit import PendingChanges

    # 每一次都重摆沙箱（`real_probe.reset_sandbox`，与那份探针同一份实现）：
    # 沙箱整批共用一份的话，第一跑改完代码之后，后面的臂就都开始于全绿的现场，
    # 而这个靶问的正是「哪条测试还红着」——臂的顺序会替模型做题。
    project = Path(reset_sandbox(state_file=STATE_FILE, stage=stage_sandbox))
    gateway = LlamaCppGateway(
        base_url=os.environ.get("ORCH_BASE_URL", "http://127.0.0.1:8080"),
        model=os.environ.get("ORCH_MODEL", "local"),
        timeout=float(os.environ.get("ORCH_HTTP_TIMEOUT", "600")),
        temperature=float(os.environ.get("ORCH_TEMPERATURE", "0")),
        seed=int(os.environ.get("ORCH_SEED", "7")),
    )
    tokenizer = counter_for(gateway)
    events: list[tuple[str, dict]] = []
    # 逐回合的原话（只在 `ORCH_DUMP_TURNS` 指了文件时才记）：诊断「收束指令
    # 发下去之后它到底回了什么」必须看这一段，事件流里的 detail 只有 200 字。
    turns: list[dict] = []
    dump_path = os.environ.get("ORCH_DUMP_TURNS", "")
    scene = scene_now()
    loop = assemble_loop(
        project,
        gateway,
        config=Config(
            project_root=project,
            supervise=False,
            max_steps=_max_steps(),
            context_window=8192,
            overrides=dict(ARMS[arm]),
        ),
        wiring=LoopWiring(
            mcp=False,
            pending=PendingChanges(project),
            on_event=lambda kind, data: events.append((kind, data)),
        ),
    )
    if dump_path:
        from dump_turns import tap_gateway

        tap_gateway(gateway, turns)
    tool_count = len(loop.registry.names())
    system_tokens = tokenizer.count(_system_text(loop))
    # S9 读数：收束期的紧步数最招重复（S8 那两批的同一个现场在这里最容易踩到
    # 「判不出就放行」那条旧通道），所以这一档也记「同一件事真的重跑了几次」。
    guard_book = instrument_guards(loop)

    started = time.perf_counter()
    error = ""
    result = None
    try:
        result = loop.run(GOALS[task])
    except Exception as exc:  # noqa: BLE001 - **不许静默**：这是失败，照记
        error = f"{type(exc).__name__}: {exc}"
    wall = time.perf_counter() - started
    gateway.close()

    tools = [data for kind, data in events if kind == "tool"]
    notes = [data for kind, data in events if kind == "note"]
    final_event = next((data for kind, data in events if kind == "final"), {})
    # 收束指令有没有真的发出去：trace 里那一行是唯一出处（模型可见文本不进
    # 事件流），所以它同时是「机制生效」的读数。
    converged = any("注入收束指令" in line for line in (result.trace if result else []))
    step_lines = [line for line in (result.trace if result else []) if "硬上限" in line]
    failed_notes = [one for one in notes if one.get("kind") == "run_failed"]
    # 收束期「写成果」的放行/拦截次数（`loop._converge_write_verdict` 打的那两条
    # note，`kind="converge"` + `action`）。默认 -1 时这两条**一次都不打**，
    # 所以默认臂读到的 0 表示「这一级没参与」，不是「一次都没写」。
    converge_notes = [one for one in notes if one.get("kind") == "converge"]
    write_allowed = sum(
        1 for one in converge_notes if one.get("action") == "write_allowed"
    )
    write_blocked = sum(
        1 for one in converge_notes if one.get("action") == "write_blocked"
    )
    guard = guard_readings(guard_book)

    final_text = result.report_text if result is not None else ""
    return {
        "label": label,
        "task": task,
        "arm": arm,
        "repeat": repeat,
        "head": scene["head"],
        "prompt_version": scene["prompt_version"],
        "tool_count": tool_count,
        "system_tokens": system_tokens,
        "max_steps": _max_steps(),
        "overrides": dict(ARMS[arm]),
        "steps": result.steps if result else None,
        "model_calls": result.model_calls if result else None,
        "prompt_tokens": result.prompt_tokens if result else None,
        "completion_tokens": result.completion_tokens if result else None,
        "wall_seconds": round(wall, 3),
        "resets": result.resets if result else None,
        "finished": bool(result.finished) if result else False,
        # ---- P0 指标（口径在 `loop.py`，这里只搬运）--------------------------
        "concluded": bool(result.concluded) if result else False,
        "model_final": result.model_final if result else "",
        "terminated_by": (result.terminated_by if result else "失败"),
        "failed_reason": (result.failed_reason or error) if result else error,
        "converge_injected": converged,
        "budget_notes": step_lines,
        "failed_notes": len(failed_notes),
        "write_allowed": write_allowed,
        "write_blocked": write_blocked,
        "repeat_executions": guard["repeat_executions"],
        "refused_useless": guard["refused_useless"],
        "refused_undecided": guard["refused_undecided"],
        "command_runs": guard["command_runs"],
        "refused_useless_tools": guard["refused_useless_tools"],
        "refused_undecided_tools": guard["refused_undecided_tools"],
        "repeat_tools": guard["repeat_tools"],
        "tool_calls": len(tools),
        # 逐回合诊断（只在 `ORCH_DUMP_TURNS` 打开时有内容）。
        "turns_tail": [
            {
                "n": one["n"],
                "converge_injected": one["converge_injected"],
                "reply_head": one["reply"][:400],
            }
            for one in turns[-3:]
        ],
        # 可用性判据读**模型自己写的结论**（不读混着系统判词的报告正文）。
        "checks": checks_for(task, result.model_final if result else ""),
        "final_text": final_text,
    }


def _failed_record(task: str, arm: str, repeat: int, label: str, exc, scene: dict) -> dict:
    """跑不起来的那一次怎么进读数（P0 修②的跑分器一侧）。

    字段与正常记录齐（报告按同一个口径取），差别只在 `concluded=False`、
    `terminated_by="失败"`、`failed_reason` 非空——这三条就是「响亮的失败」。
    """
    return {
        "label": label,
        "task": task,
        "arm": arm,
        "repeat": repeat,
        "head": scene.get("head", ""),
        "prompt_version": scene.get("prompt_version", ""),
        "tool_count": scene.get("tool_count"),
        "system_tokens": scene.get("system_tokens"),
        "max_steps": _max_steps(),
        "overrides": dict(ARMS[arm]),
        "steps": None,
        "model_calls": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "wall_seconds": 0.0,
        "resets": None,
        "finished": False,
        "concluded": False,
        "model_final": "",
        "terminated_by": "失败",
        "failed_reason": f"{type(exc).__name__}: {exc}",
        "converge_injected": False,
        "budget_notes": [],
        "failed_notes": 1,
        "write_allowed": 0,
        "write_blocked": 0,
        "repeat_executions": 0,
        "refused_useless": 0,
        "refused_undecided": 0,
        "command_runs": 0,
        "refused_useless_tools": {},
        "refused_undecided_tools": {},
        "repeat_tools": {},
        "tool_calls": 0,
        "checks": checks_for(task, ""),
        "final_text": "",
    }


def _summary(readings: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for task in GOALS:
        for arm in ARMS:
            rows = [one for one in readings if one["task"] == task and one["arm"] == arm]
            if not rows:
                continue
            checks = [one["checks"] for one in rows]
            out[f"{task}/{arm}"] = {
                "runs": len(rows),
                "concluded": sum(1 for one in rows if one["concluded"]),
                "finished": sum(1 for one in rows if one["finished"]),
                "converge_injected": sum(1 for one in rows if one["converge_injected"]),
                "write_allowed": sum(one.get("write_allowed", 0) for one in rows),
                "write_blocked": sum(one.get("write_blocked", 0) for one in rows),
                "repeat_executions": sum(one.get("repeat_executions", 0) for one in rows),
                "refused_useless": sum(one.get("refused_useless", 0) for one in rows),
                "refused_undecided": sum(one.get("refused_undecided", 0) for one in rows),
                "command_runs": sum(one.get("command_runs", 0) for one in rows),
                "terminated_by": _tally(one["terminated_by"] for one in rows),
                "steps_median": statistics.median([one["steps"] or 0 for one in rows]),
                "prompt_tokens_median": statistics.median(
                    [one["prompt_tokens"] or 0 for one in rows]
                ),
                "checks": {
                    key: sum(1 for one in checks if one.get(key))
                    for key in checks[0]
                    # `_` 开头的一律是明细（`_hits` / `_paths`），不是判据。
                    if not key.startswith("_")
                },
                "checks_of": len(checks),
            }
    return out


def _tally(items) -> dict:
    out: dict[str, int] = {}
    for one in items:
        out[one] = out.get(one, 0) + 1
    return out


def _flush(readings: list[dict], skipped: list[dict], scene: dict, started: float) -> None:
    merged = readings
    OUT.write_text(
        json.dumps(
            {
                "kind": "P0 off-arm convergence probe",
                "provider": os.environ.get("ORCH_BASE_URL", "http://127.0.0.1:8080"),
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "scene": scene,
                "budget_seconds": _budget(),
                "repeats": _repeats(),
                "arms": ARMS,
                "goals": GOALS,
                "local_http_isolated": True,
                "no_proxy": os.environ.get("NO_PROXY"),
                "readings": merged,
                "skipped": skipped,
                "summary": _summary(merged),
                "elapsed_seconds": round(time.time() - started, 1),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def stage_run() -> int:
    _isolate_local_http()
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    scene = scene_now()
    budget = _budget()
    repeats = _repeats()
    tasks = _tasks()
    arms = _settings()
    say(f"\n===== P0 真机小样本 @ {time.strftime('%Y-%m-%d %H:%M:%S')} =====")
    say(f"版本 : HEAD {scene['head'][:12]} PROMPT_VERSION {scene['prompt_version']} "
        f"工具 {scene['tool_count']} 件 system {scene['system_tokens']} token "
        f"（有效预算 {scene['effective_tokens']}）")
    say(f"预算 : {budget}s；任务 {tasks}；臂 {arms}；每格 {repeats} 次；"
        f"max_steps {_max_steps()}")
    say(f"本机回环隔离代理：NO_PROXY={os.environ.get('NO_PROXY')}；"
        f"HTTP_PROXY={'（已清空）' if not os.environ.get('HTTP_PROXY') else os.environ['HTTP_PROXY']}")
    say(f"沙箱 : {state['project']}（失败测试 exit={state['fail_exit']}）")

    readings: list[dict] = []
    skipped: list[dict] = []
    started = time.time()
    out_of_budget = False
    for arm in arms:
        for task in tasks:
            for repeat in range(repeats):
                label = f"{task}|{arm}|r{repeat}"
                elapsed = time.time() - started
                if elapsed >= budget:
                    # **先减重复，不减任务**：撞线之后剩下的每一次都记一条跳过
                    # （读数里说得清「为什么只跑了这么几次」）。
                    skipped.append({"label": label, "reason": "整批预算已用尽"})
                    say(f"{label}: 跳过（整批预算已用尽 {elapsed:.0f}s / {budget}s）")
                    out_of_budget = True
                    continue
                say(f"{label}: 开跑（已用 {elapsed:.0f}s / {budget}s）…")
                try:
                    record = run_once(task, arm, repeat, label)
                except Exception as exc:  # noqa: BLE001 - **不许静默**
                    # 服务/网关那一步坏了**不是「没做完」**：它必须进读数
                    # （`terminated_by="失败"`），而不是被丢进 `skipped`。
                    # 上一轮那批 502 就是被丢进 `skipped` 的——36 条读数里
                    # 一条失败都看不到，收敛率的分母里也没有它们。
                    record = _failed_record(task, arm, repeat, label, exc, scene)
                    say(
                        f"  ！！失败：{record['failed_reason'][:140]}"
                        "（这一次记成失败，不是「未完成」）"
                    )
                readings.append(record)
                passed = [k for k, v in record["checks"].items() if k != "_hits" and v]
                say(
                    f"  步 {record['steps']} 输入 {record['prompt_tokens']} "
                    f"输出 {record['completion_tokens']} 墙钟 {record['wall_seconds']:.1f}s "
                    f"工具调用 {record['tool_calls']} 重置 {record['resets']} "
                    f"收束={'是' if record['converge_injected'] else '否'} "
                    f"结论={'有' if record['concluded'] else '无'} "
                    f"终止={record['terminated_by']} 可用 {len(passed)}/"
                    f"{len(record['checks']) - 1}"
                    + (f" ！！{record['failed_reason'][:100]}"
                       if record["failed_reason"] else "")
                )
                if record["budget_notes"]:
                    for one in record["budget_notes"]:
                        say(f"    {one}")
                _flush(readings, skipped, scene, started)
    _flush(readings, skipped, scene, started)
    say(f"===== 跑完（跳过 {len(skipped)} 次）=====")
    return 0 if not out_of_budget else 0


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else "run"
    if action == "sandbox":
        return stage_sandbox()
    if action == "scenes":
        print(json.dumps(scene_now(), ensure_ascii=False, indent=2))
        return 0
    if action == "clean":
        if STATE_FILE.is_file():
            base = Path(json.loads(STATE_FILE.read_text(encoding="utf-8"))["project"]).resolve().parent
            if "spool-p0-converge-" in base.name:
                shutil.rmtree(base, ignore_errors=True)
                print(f"已删 {base}")
                return 0
        print("没什么可删的")
        return 0
    return stage_run()


if __name__ == "__main__":
    raise SystemExit(main())
