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

from spoolkit.config import Config

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
        prefix="spool-bench-", ignore_cleanup_errors=True
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
            # 时间拆成模型/工具两笔：这是「为什么这次慢」唯一能查的东西。
            print(f"      用量：{result.usage()}")

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


# --- 第二条仪器：整批铺成一个工作区，全部交给一条会话 ---------------------
#
# 上面那条（一题一会话）量的是**单题能力**，而它有个结构性盲区：
# 真实使用里没有「一道题一个新上下文」这回事。真实长这样——一个工作区、
# 一堆没做完的事、一个必须自己记住做到哪儿了的会话。
#
# 所以这里再铺一条：把全部题放进一个工作区，告诉它题目在哪儿，让它自己
# 去读、自己排序、自己验、自己收尾。量的是另外几件事：
#   - 任务发现：能不能自己从目录里读出「这题要干什么」；
#   - 自我排序：做完一道能不能接着开下一道，而不是原地绕；
#   - 长程：上下文重置之后还记不记得做到哪儿（状态块扛不扛得住）；
#   - 派发与督导：长任务里这两个机制第一次真正被用上。
#
# **两条都要留着**：一条掉了分，另一条未必动。只看一条会得出相反的结论。

TOGETHER_GOAL = """这个工作区里放着 {count} 道编程题，每道题一个子目录。

每道题目录里有：
- TASK.md：这道题要做什么、怎么算做完；
- 起始代码；
- 一个 test_ 打头的验收测试。**动手前先读它**——它固定了这个模块对外的样子
  （导入哪个名字、怎么调用、返回什么）；**也不要改它**，验收时会用原始副本
  覆盖回来，改了不算数。

要求：
- 怎么安排由你决定（自己做、或者把一批一起交出去做）；
- 每道题做完要能自己验：在那个目录里跑 pytest 确认通过，再往下走；
- 不要重做已经做完的；
- **尽量多做**：除非预算真的耗尽、或者确实卡住做不下去，否则不要停。
  收尾时用 final 报告：做完了哪几道、剩下的为什么没做。
"""


def prepare_together(tasks: list[Task], workspace: Path) -> None:
    """把全部任务铺成一个工作区。

    刻意**不给题目清单**：目录里有什么，让它自己去列、自己去读。
    那正是这条仪器要量的东西，替它列出来就把要量的东西量没了。
    """
    workspace.mkdir(parents=True, exist_ok=True)
    for task in tasks:
        home = workspace / task.name
        prepare(task, home)
        _rename_acceptance(task, home)
        command = " ".join(task.command) or "（这道题没声明验收命令）"
        (home / "TASK.md").write_text(
            f"# {task.name}\n\n{task.goal}\n\n"
            f"做完的判定：在这个目录里执行 `{command}`，全部通过。\n"
            "test_acceptance.py 是验收标准，不要改它——**动手前先读它**：\n"
            "它固定了这个模块**对外长什么样**（导入哪个名字、函数怎么调用、"
            "返回什么）。把代码改得更「合理」但动了接口，就算改坏了。\n",
            encoding="utf-8",
        )


def _unique_test_name(task: Task, name: str) -> str:
    return f"test_{task.name}.py" if name.endswith(".py") else f"{task.name}_{name}"


def _rename_acceptance(task: Task, workdir: Path) -> None:
    """把验收测试改成**工作区内唯一**的名字。

    每个题目录里的测试文件本来都叫 `test_acceptance.py`。铺在一起之后，
    在根目录跑一次 pytest 会撞「import file mismatch」（同名模块），
    而那是布局的坑、不是题目的难点——实测模型为此烧掉二十来步去「修」
    一个它修不了的东西，最后被督导判成死循环。改名之后根目录也能跑，
    它还能一眼看到「哪几道通过、哪几道没过」。
    """
    for name in task.acceptance_files:
        source = workdir / name
        if source.is_file():
            source.rename(workdir / _unique_test_name(task, name))


def verify_together(tasks: list[Task], workspace: Path) -> list[TaskResult]:
    """逐题验收。用的是同一条路径：原始验收测试覆盖回去，只看退出码。"""
    results: list[TaskResult] = []
    for task in tasks:
        home = workspace / task.name
        # 覆盖回**改名之后**那一份：模型改测试想蒙混过关的话，同样作废。
        for name in task.acceptance_files:
            source = task.home / "acceptance" / name
            if source.is_file():
                shutil.copy(source, home / _unique_test_name(task, name))
        passed, detail = verify(task, home)
        results.append(
            TaskResult(
                name=task.name,
                passed=passed,
                agent_finished=False,
                steps=0,
                prompt_tokens=0,
                model_calls=0,
                seconds=0.0,
                detail=detail,
            )
        )
    return results


def render_together(results: list[TaskResult], meta: dict) -> str:
    passed = [item for item in results if item.passed]
    failed = [item for item in results if not item.passed]
    lines = [
        f"整批一条会话：验收通过 {len(passed)}/{len(results)}",
        f"这条会话：{meta['steps']} 步 / {meta['calls']} 次调用 / "
        # 输出 token 也要记：光有输入看不出「时间花在生成上还是工具上」，
        # 而这两者的优化方向完全不同。
        f"{meta['prompt_tokens']} 输入 + {meta.get('completion_tokens', 0)} 输出 token / "
        f"{meta['seconds']}s",
        f"工作区留在 {meta['workspace']}（可以进去看它实际改成了什么样）",
    ]
    if failed:
        lines.append("没过的：" + "、".join(item.name for item in failed))
    return "\n".join(lines)
