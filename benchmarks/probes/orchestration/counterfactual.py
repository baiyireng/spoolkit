"""编排工具的反事实对照：**禁用编排（一次一调）vs 启用编排（一步多调）**。

## 两个任务（都是「代码形状」的取数 + 归纳）

- **A**：把仓库所有 TODO 汇总并分类、给优先级；
- **B**：定位这个失败测试涉及哪些文件并给出修复计划。

## 两个臂

- `off`：**现有路径**——模型每一步发一个工具调用，结果一份一份回灌；
- `on`：**编排**——模型一步发一份 `orchestrate` 计划，同一批调用由它执行并
  聚合成一段回灌。

## 不许 cherry-pick 的那条保证

两个臂跑的是**同一批工具调用**（`TASKS[*]["calls"]`，逐字同一份清单），
唯一的差别是「一次发几个」。脚本会核对：两个臂真的执行了同样多的调用
（`on` 那边数 `subcall` 事件，`off` 那边数 `tool` 事件）。

## 读数

`步数 / 模型调用 / 输入 token / 输出 token / 墙钟 / 工具耗时 / 结果可用性`，
每臂重复 3 次（取中位数，原始读数全量落 `readings.json`）。

## 必须说清的一点（不许粉饰）

provider 是**脚本化假模型**（不真调模型、不碰 8080）：两个臂的「脚本形状」
本身就是自变量，所以**步数与模型调用次数的差别是定义出来的**，不是模型自己
选的。这个对照真正量得准的是三件事：

1. **同一批调用回灌给模型的总量**（输入 token、最后一次请求的上下文大小）——
   它只取决于「几次发完」，与脚本是谁写的无关；
2. **工具侧的墙钟**（只读并行 vs 一次一个）——真的在盘上跑 rg / 读文件；
3. **模型可见的素材够不够写出结论**（结果可用性清单）。

`结果可用性` 量的是**素材**（命中位置、状态行、§2.5 条款有没有进到最后一次
请求里），不是最终答复的质量——静态脚本写不出「读完之后才知道的内容」。
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(REPO / "scripts"))

from spoolkit import sandbox_dirs  # noqa: E402
from spoolkit.cli.runtime import LoopWiring, assemble_loop  # noqa: E402
from spoolkit.config import Config  # noqa: E402
from spoolkit.llm.fake import FakeModel  # noqa: E402
from spoolkit.llm.tokenizer import OfflineTokenCounter  # noqa: E402
from spoolkit.tools.edit import PendingChanges  # noqa: E402
from spoolkit.tools.orchestrate import EVENT_SUBCALL, REVERSE_CLAUSE  # noqa: E402

OUT = Path(__file__).resolve().parent
REPEATS = int(os.environ.get("ORCH_REPEATS", "3"))


# ---- 任务（两个臂共用同一批调用）-------------------------------------------


#: 沙箱里预置的待办标记。**为什么要有这一步**：这个仓库里几乎没有 `TODO`
#: 字面量（`rg "TODO|FIXME|XXX" src/spoolkit` 只有 1 条误命中：`.env.XXXX.tmp`
#: 那个注释），任务 A 若按原样跑，五次检索里四次空手而归——那样的对照量不出
#: 「取数 + 归纳」的成本，只会量到「没东西可取」。所以沙箱里按同一份配方预置
#: 6 条待办（两个臂拿到的是**逐字相同**的沙箱），并在报告里写明这一点。
SEED_MARK = "TODO(orchestration-bench)"
SEED_FILES = (
    "src/spoolkit/cli/runtime.py",
    "src/spoolkit/tools/registry.py",
    "src/spoolkit/tools/capabilities.py",
    "src/spoolkit/agent/audit.py",
    "src/spoolkit/tools/exec.py",
    "src/spoolkit/tools/fs.py",
)


def _c(tool: str, args: dict, why: str) -> dict:
    return {"tool": tool, "args": args, "why": why}


TASKS: dict[str, dict] = {
    "A-todo": {
        "goal": "把仓库所有 TODO 汇总并分类、给优先级",
        "why": "一次把待办捞全、再看两处上下文，然后分类排序",
        "aggregate": "按「要做的事」归类，给出优先级与出处（文件:行）",
        "calls": [
            _c("search_code", {"pattern": "TODO|FIXME|XXX", "path": "src/spoolkit", "max_results": 8}, "把 src 里的待办全捞出来"),
            _c("search_code", {"pattern": "TODO|FIXME|XXX", "path": "tests", "max_results": 8}, "测试里的待办也要算"),
            _c("search_code", {"pattern": "抽成独立函数", "path": "src/spoolkit", "max_results": 8}, "按用途再捞一遍"),
            _c("search_code", {"pattern": "def orchestrate_spec|def run_plan", "path": "src/spoolkit/tools", "max_results": 8}, "定位编排那两个入口"),
            _c("read_file", {"path": "src/spoolkit/tools/orchestrate.py", "start_line": 1, "end_line": 160}, "看一处待办的上下文"),
        ],
        "final": (
            "TODO 汇总（分类 + 优先级 + 出处）：\n"
            "1. 高：编排与护栏相关（见检索命中的 文件:行）\n"
            "2. 中：测试与文档类\n"
            "3. 低：注释里的历史遗留\n"
            "以上每条都带命中的 文件:行，可逐条核对。"
        ),
        # 两个臂都必须拿到的素材（用来写结论的那几样）。**只认工具输出里
        # 才会出现的东西**：模型自己回显的调用参数里也有 "TODO" 字样，
        # 拿它当判据等于自证。
        "needs": (f"# {SEED_MARK}",),
        # 分级读数：最后一次请求里能找到**几处**预置待办（6 处里）。
        # 「够不够写结论」按 ≥2 处算——分类与排序至少要看到两条。
        "count_of": tuple(f"{one.split('/')[-1]}:{index + 1}" for index, one in enumerate(SEED_FILES)),
        "count_needles": tuple(one.split("/")[-1] for one in SEED_FILES),
        "count_at_least": 2,
    },
    "B-failing-test": {
        "goal": "定位这个失败测试涉及哪些文件并给出修复计划",
        "why": "一次把失败测试、命令层与实现层都取回来，再给修复计划",
        "aggregate": "列出涉及的文件（去重）与修复步骤",
        "calls": [
            _c("read_file", {"path": "tests/cli/test_service_command.py", "start_line": 1, "end_line": 160}, "看失败测试本体"),
            _c("search_code", {"pattern": "taskkill", "path": "src/spoolkit", "max_results": 8}, "谁在 taskkill"),
            _c("search_code", {"pattern": "def _service|def service", "path": "src/spoolkit/cli", "max_results": 8}, "服务命令的入口在哪"),
            _c("search_code", {"pattern": "service", "path": "src/spoolkit/cli/commands", "max_results": 8}, "命令层哪些文件提到 service"),
            _c("read_file", {"path": "src/spoolkit/cli/commands/service.py", "start_line": 1, "end_line": 160}, "读实现层"),
        ],
        "final": (
            "失败测试涉及的文件：\n"
            "1. tests/cli/test_service_command.py（断言侧）\n"
            "2. src/spoolkit/cli/commands/service.py（实现侧）\n"
            "3. src/spoolkit/cli/runtime.py（装配侧）\n"
            "修复计划：先核对 taskkill 的调用点，再让断言只看退出码与输出。"
        ),
        "needs": ("test_service_command.py", "service.py"),
        "count_of": (),
        "count_needles": (),
        "count_at_least": 0,
    },
}


def _turn(calls: list[dict], thought: str, done: bool = False, final: str | None = None) -> str:
    return json.dumps(
        {
            "thought": thought,
            "tool_calls": calls,
            "done": done,
            "final": final,
        },
        ensure_ascii=False,
    )


def _off_script(task: dict) -> list[str]:
    """**现有路径**：每一步发一个调用，再给结论。"""
    script = [
        _turn(
            [
                {
                    "name": call["tool"],
                    "arguments": call["args"],
                    "why": call["why"],
                }
            ],
            f"取第 {index + 1} 处（一次一个）",
        )
        for index, call in enumerate(task["calls"])
    ]
    script.append(_turn([], "汇总", done=True, final=task["final"]))
    return script


def _on_script(task: dict) -> list[str]:
    """**启用编排**：一步发一整份计划，再给结论。"""
    plan = {
        "why": task["why"],
        "calls": [
            {
                "id": f"c{index + 1}",
                "tool": call["tool"],
                "args": call["args"],
                "why": call["why"],
                "needs": [],
            }
            for index, call in enumerate(task["calls"])
        ],
        "aggregate": task["aggregate"],
    }
    return [
        _turn(
            [{"name": "orchestrate", "arguments": {"plan": plan}, "why": task["why"]}],
            "一步编排",
        ),
        _turn([], "汇总", done=True, final=task["final"]),
    ]


# ---- 沙箱 ------------------------------------------------------------------


#: 沙箱里预置的待办标记。**为什么要有这一步**：这个仓库里几乎没有 `TODO`
#: 字面量（`rg "TODO|FIXME|XXX" src/spoolkit` 只有 1 条误命中：`.env.XXXX.tmp`
#: 那个注释），任务 A 若按原样跑，五次检索里四次空手而归——那样的对照量不出
#: 「取数 + 归纳」的成本，只会量到「没东西可取」。所以沙箱里按同一份配方预置
#: 6 条待办（两个臂拿到的是**逐字相同**的沙箱），并在报告里写明这一点。
SEED_MARK = "TODO(orchestration-bench)"
SEED_FILES = (
    "src/spoolkit/cli/runtime.py",
    "src/spoolkit/tools/registry.py",
    "src/spoolkit/tools/capabilities.py",
    "src/spoolkit/agent/audit.py",
    "src/spoolkit/tools/exec.py",
    "src/spoolkit/tools/fs.py",
)


def _seed_todos(project: Path) -> int:
    """按同一份配方往沙箱里写 6 条待办（两个臂拿到的是逐字相同的沙箱）。"""
    seeded = 0
    for index, relative in enumerate(SEED_FILES):
        target = project / relative
        if not target.is_file():
            continue
        with target.open("a", encoding="utf-8") as handle:
            handle.write(
                f"\n# {SEED_MARK}: 第 {index + 1} 处待办——把这一段抽成独立函数\n"
            )
        seeded += 1
    return seeded


def _make_sandbox() -> Path:
    """把被测代码复制进一个临时工作区（绝不在仓库里跑、也不留下一份 .agent）。"""
    base = sandbox_dirs.make_accessible_temp_dir("spool-orch-cf-")
    project = base / "project"
    ignore = shutil.ignore_patterns(
        "__pycache__", ".pytest_cache", ".git", ".agent", "*.pyc"
    )
    shutil.copytree(SRC / "spoolkit", project / "src" / "spoolkit", ignore=ignore)
    (project / "tests" / "cli").mkdir(parents=True)
    shutil.copy2(
        REPO / "tests" / "cli" / "test_service_command.py",
        project / "tests" / "cli" / "test_service_command.py",
    )
    _seed_todos(project)
    return project


def _add_rg_to_path() -> str:
    """rg 不在 PATH 里时补一次（候选目录沿用 `scripts/probe.py` 那一份）。"""
    if shutil.which("rg") is not None:
        return "PATH 里已有"
    try:
        import probe  # type: ignore[import-not-found]

        dirs = probe._tool_dirs()
    except Exception:
        local = os.environ.get("LOCALAPPDATA") or ""
        dirs = [
            Path(local) / "SummerEngine" / "current" / "ripgrep" / "bin",
            Path(local) / "OpenAI" / "Codex" / "bin",
            Path(local) / "Microsoft" / "WinGet" / "Links",
            Path("C:/Program Files/ripgrep"),
        ]
    for directory in dirs:
        if (Path(directory) / "rg.exe").is_file():
            os.environ["PATH"] = f"{directory}{os.pathsep}{os.environ.get('PATH', '')}"
            return str(directory)
    return "找不到（search_code 会失败）"


def _run_once(project: Path, name: str, task: dict, arm: str) -> dict:
    tokenizer = OfflineTokenCounter()
    script = _off_script(task) if arm == "off" else _on_script(task)
    model = FakeModel(script=script, tokenizer=tokenizer)
    pending = PendingChanges(project)
    events: list[tuple[str, dict]] = []
    wiring_started = time.perf_counter()
    loop = assemble_loop(
        project,
        model,
        config=Config(
            project_root=project, supervise=False, max_steps=12, context_window=8192
        ),
        wiring=LoopWiring(mcp=False, pending=pending, on_event=lambda k, d: events.append((k, d))),
    )
    wiring_seconds = time.perf_counter() - wiring_started

    started = time.perf_counter()
    result = loop.run(task["goal"])
    wall = time.perf_counter() - started

    last_request = model.requests[-1] if model.requests else None
    last_text = (
        "\n".join(message.content for message in last_request.messages)
        if last_request is not None
        else ""
    )
    # 素材清单：**两个臂都量同一份判据**（写结论要用的那几样在不在最后一次
    # 请求里）。编排臂另有一组「只有编排才提供」的素材，单列上报、不混进
    # 通过/不通过——那是这个工具的功能本身，不是两个臂的共同判据。
    checks = {
        f"素材：{needle}": needle in last_text for needle in task["needs"]
    }
    hit_files = [one for one in task["count_needles"] if one in last_text]
    checks[f"素材：可定位的待办处数 ≥{task['count_at_least']}"] = (
        len(hit_files) >= task["count_at_least"]
    )
    if os.environ.get("ORCH_DEBUG"):
        dump = REPO / ".tmp" / f"orch-cf-{name}-{arm}.txt"
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.write_text(last_text, encoding="utf-8")
        print(f"  [debug] 最后一次请求 {len(last_text)} 字 → {dump}")
    materials = {
        "每条分支的状态行": "调用链" in last_text,
        "每条子调用的意图（why）": "意图：" in last_text,
        "§2.5 反向条款": REVERSE_CLAUSE in last_text,
    } if arm == "on" else {}

    executed = (
        len([1 for kind, _ in events if kind == EVENT_SUBCALL])
        if arm == "on"
        else len(
            [
                1
                for kind, data in events
                if kind == "tool" and data.get("name") not in ("自动验证", "最终验证")
            ]
        )
    )
    return {
        "arm": arm,
        "task": name,
        "steps": result.steps,
        "model_calls": result.model_calls,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "wall_seconds": round(wall, 3),
        "tool_seconds": round(result.tool_seconds, 3),
        "wiring_seconds": round(wiring_seconds, 3),
        "requests": len(model.requests),
        "last_request_chars": len(last_text),
        "executed_tool_calls": executed,
        "found_seed_hits": len(hit_files),
        "finished": result.finished,
        "availability": checks,
        "availability_pass": all(checks.values()),
        "orchestration_materials": materials,
    }


def main() -> int:
    rg = _add_rg_to_path()
    print(f"repo   : {REPO}")
    print(f"rg     : {rg}")
    print(f"重复   : {REPEATS} 次/臂\n")
    readings: list[dict] = []
    for name, task in TASKS.items():
        for arm in ("off", "on"):
            for repeat in range(REPEATS):
                project = _make_sandbox()
                try:
                    record = _run_once(project, name, task, arm)
                finally:
                    shutil.rmtree(project.parent, ignore_errors=True)
                record["repeat"] = repeat
                readings.append(record)
                print(
                    f"{name:14s} {arm:3s} r{repeat}: 步数 {record['steps']:2d} "
                    f"模型调用 {record['model_calls']:2d} 输入 {record['prompt_tokens']:6d} "
                    f"输出 {record['completion_tokens']:4d} 墙钟 {record['wall_seconds']:.2f}s "
                    f"工具 {record['tool_seconds']:.2f}s 末次上下文 {record['last_request_chars']:6d} 字 "
                    f"调用数 {record['executed_tool_calls']} 可用 {'✔' if record['availability_pass'] else '✘'}"
                )

    summary: dict[str, dict] = {}
    for name in TASKS:
        for arm in ("off", "on"):
            rows = [one for one in readings if one["task"] == name and one["arm"] == arm]
            summary[f"{name}/{arm}"] = {
                key: round(statistics.median(one[key] for one in rows), 3)
                for key in (
                    "steps",
                    "model_calls",
                    "prompt_tokens",
                    "completion_tokens",
                    "wall_seconds",
                    "tool_seconds",
                    "last_request_chars",
                    "executed_tool_calls",
                    "found_seed_hits",
                )
            }
            summary[f"{name}/{arm}"]["availability_pass"] = all(
                one["availability_pass"] for one in rows
            )
            materials = [one["orchestration_materials"] for one in rows if one["orchestration_materials"]]
            if materials:
                summary[f"{name}/{arm}"]["orchestration_materials"] = {
                    key: all(one[key] for one in materials) for key in materials[0]
                }

    print("\n中位数对照：")
    for name in TASKS:
        off = summary[f"{name}/off"]
        on = summary[f"{name}/on"]
        print(
            f"  {name}: 步数 {off['steps']} → {on['steps']}，"
            f"模型调用 {off['model_calls']} → {on['model_calls']}，"
            f"输入 token {off['prompt_tokens']} → {on['prompt_tokens']} "
            f"({(on['prompt_tokens'] / off['prompt_tokens'] - 1) * 100:+.1f}%)，"
            f"输出 token {off['completion_tokens']} → {on['completion_tokens']}，"
            f"墙钟 {off['wall_seconds']:.2f}s → {on['wall_seconds']:.2f}s，"
            f"工具 {off['tool_seconds']:.2f}s → {on['tool_seconds']:.2f}s，"
            f"末次上下文 {off['last_request_chars']} → {on['last_request_chars']} 字，"
            f"执行调用数 {off['executed_tool_calls']} vs {on['executed_tool_calls']}"
        )

    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "provider": "FakeModel（脚本化假模型，不真调模型）",
        "repeats": REPEATS,
        "rg": rg,
        "tasks": {name: {"goal": task["goal"], "calls": task["calls"]} for name, task in TASKS.items()},
        "readings": readings,
        "summary": summary,
    }
    target = OUT / "readings.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n原始读数已写入 {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
