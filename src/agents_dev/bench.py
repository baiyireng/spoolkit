"""回归任务集。

存在的理由是：**没有它，所有调参都是盲调。**

前面几轮我反复尝到这个苦头——改提示词、改超时、改配额，每次只能拿一次
基线样本对照，说出来的结论都得带一句「样本不足」。那不是测量，那是感觉。

一个任务由三部分组成：
- `project/`：agent 看到的起始代码；
- `task.json`：目标与验收命令；
- `acceptance/`：验收测试。

验收测试**放进工作区**，但**验收前会用原始副本覆盖回去**。

这两条缺一不可。藏起来的话，agent 无法自己跑测试验证——而「改完能自己
知道对不对」正是这套系统最核心的能力，回归集要量的就是它。可要是不还原，
agent 改测试让测试通过就能拿分，那是作弊，而且很难发现。

所以：给它看，但说了不算。

判定只看验收命令的退出码。不看 agent 自己说做完了没有，也不看它写得漂不漂亮。
"""

import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from agents_dev.config import Config

DEFAULT_BENCH_ROOT = "benchmarks/tasks"
DEFAULT_TIMEOUT = 300


@dataclass(frozen=True)
class Task:
    """一个回归任务。"""

    name: str
    goal: str
    command: tuple[str, ...]
    acceptance_files: tuple[str, ...] = ()
    scope: tuple[str, ...] = ()
    home: Path = field(default=Path("."))


@dataclass
class TaskResult:
    """一次任务的结果。"""

    name: str
    passed: bool
    agent_finished: bool
    steps: int
    prompt_tokens: int
    model_calls: int
    seconds: float
    detail: str = ""


def load_tasks(bench_root: Path) -> list[Task]:
    """读取全部任务，按名字排序。"""
    tasks: list[Task] = []
    if not bench_root.exists():
        return tasks
    for home in sorted(bench_root.iterdir()):
        spec_path = home / "task.json"
        if not spec_path.is_file():
            continue
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        acceptance = spec.get("acceptance") or {}
        tasks.append(
            Task(
                name=home.name,
                goal=spec["goal"],
                command=tuple(acceptance.get("command") or ()),
                acceptance_files=tuple(acceptance.get("tests") or ()),
                scope=tuple(spec.get("scope") or ()),
                home=home,
            )
        )
    return tasks


def prepare(task: Task, workdir: Path) -> None:
    """把起始代码与验收测试一起复制进工作区。"""
    source = task.home / "project"
    if not source.exists():
        workdir.mkdir(parents=True, exist_ok=True)
    else:
        shutil.copytree(source, workdir)
    _copy_acceptance(task, workdir)


def _copy_acceptance(task: Task, workdir: Path) -> None:
    for name in task.acceptance_files:
        shutil.copy(task.home / "acceptance" / name, workdir / name)


def verify(task: Task, workdir: Path) -> tuple[bool, str]:
    """用原始验收测试覆盖工作区里的同名文件，再跑验收命令。只看退出码。

    覆盖这一步是关键：agent 可能为了让它通过而改测试，覆盖之后那些改动
    一律作废，验收结果只反映它对被测代码做了什么。
    """
    _copy_acceptance(task, workdir)

    if not task.command:
        return False, "任务没有声明验收命令"

    # 命令里的 python / pytest 指向当前解释器。任务文件是数据，
    # 不该要求写它的人知道这台机器上解释器在哪。
    command = list(task.command)
    if command and command[0] in ("python", "python3"):
        command = [sys.executable, *command[1:]]
    elif command and command[0] == "pytest":
        command = [sys.executable, "-m", "pytest", *command[1:]]

    environment = dict(**__import__("os").environ)
    environment["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(
            command,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=DEFAULT_TIMEOUT,
            env=environment,
        )
    except subprocess.TimeoutExpired:
        return False, f"验收命令超过 {DEFAULT_TIMEOUT} 秒"
    except FileNotFoundError as exc:
        return False, f"验收命令无法执行: {exc}"

    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode == 0, output[-600:]


def run_task(
    task: Task,
    build_loop,
    workdir: Path | None = None,
    settle_changes=None,
    verbose: bool = False,
) -> TaskResult:
    """跑一个任务并验收。

    build_loop 由调用方提供：它负责用哪个模型、哪个工作区装配一个循环。
    这样回归测试本身不绑定任何具体模型。
    """
    started = time.time()
    # 忽略清理错误：索引数据库的连接在整个进程里都开着（SQLite 在 Windows 上
    # 会锁住文件），硬要在退出时删掉只会把结果一起吞掉。临时目录留给系统回收。
    with tempfile.TemporaryDirectory(
        prefix="agents-bench-", ignore_cleanup_errors=True
    ) as temp:
        workspace = workdir or Path(temp) / "project"
        if workdir is None:
            prepare(task, workspace)

        loop = build_loop(workspace)
        result = loop.run(task.goal)
        # 回归环境里没有人工确认，改动必须自动落盘——否则验收测的是
        # 没被改过的旧代码，所有任务都会「失败」，而原因不在模型。
        if settle_changes is not None:
            settle_changes()

        if verbose:
            # 回归集本身也会出错（工作区搭错、改动没落盘、测试没进去）。
            # 没有这层输出，你只能看到「失败」，然后开始怀疑模型。
            print("      轨迹：")
            for line in result.trace:
                print(f"        {line}")
            print(f"      产出：{(result.final or '')[:300]}")

        passed, detail = verify(task, workspace)
        if verbose and not passed:
            print("      验收输出：")
            for line in detail.splitlines():
                print(f"        {line}")

        return TaskResult(
            name=task.name,
            passed=passed,
            agent_finished=result.finished,
            steps=result.steps,
            prompt_tokens=result.prompt_tokens,
            model_calls=result.model_calls,
            seconds=round(time.time() - started, 1),
            detail=detail,
        )


def render_report(results: list[TaskResult]) -> str:
    """汇总成一张表。"""
    if not results:
        return "（没有任务）"

    lines = [
        f"{'任务':<24}{'验收':<6}{'自称完成':<10}{'步数':>5}"
        f"{'调用':>6}{'输入token':>11}{'耗时':>7}"
    ]
    for item in results:
        lines.append(
            f"{item.name:<24}{'通过' if item.passed else '失败':<6}"
            f"{'是' if item.agent_finished else '否':<10}"
            f"{item.steps:>5}{item.model_calls:>6}{item.prompt_tokens:>11}"
            f"{item.seconds:>6}s"
        )

    total = len(results)
    passed = sum(1 for item in results if item.passed)
    claimed = sum(1 for item in results if item.agent_finished)
    average_steps = sum(item.steps for item in results) / total
    average_tokens = sum(item.prompt_tokens for item in results) / total

    lines.append("")
    lines.append(f"验收通过 {passed}/{total}（{passed / total:.0%}）")
    lines.append(f"自称完成 {claimed}/{total}")
    lines.append(f"平均步数 {average_steps:.1f}，平均输入 {average_tokens:.0f} token")

    # 「自称完成但验收失败」是最值得盯的一类：模型以为自己做到了，
    # 而客观信号说没有。这个数字高，说明验证机制没起作用。
    overconfident = [
        item for item in results if item.agent_finished and not item.passed
    ]
    if overconfident:
        lines.append(
            f"⚠ 自称完成但验收失败 {len(overconfident)} 个："
            + "、".join(item.name for item in overconfident)
        )
    return "\n".join(lines)


def resolve_config(workdir: Path, window: int, max_steps: int) -> Config:
    """回归任务用的配置。步数给得比日常宽一些，因为要量的不是省钱。"""
    return Config(project_root=workdir, context_window=window, max_steps=max_steps)
