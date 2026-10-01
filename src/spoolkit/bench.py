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

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from spoolkit import sandbox_dirs
from spoolkit.config import Config
from spoolkit.interpreter import (
    choose_interpreter,
    resolve_python_argv,
    spoolkit_checkout_root,
    venv_python,
)

DEFAULT_BENCH_ROOT = "benchmarks/tasks"
DEFAULT_TIMEOUT = 300


class WorkspaceUnavailableError(RuntimeError):
    """任务工作区在当前环境里用不了。

    与「任务失败」分开：这是**环境**的问题（目录权限被限制），不是题目、
    也不是模型的问题。混在一起的话，报告上只会多一个「失败」，而人会开始
    怀疑模型——所以它一路抛到 CLI，由那边给一句人话。
    """


def describe_workspace_error(exc: PermissionError, path: Path) -> str:
    """把「环境不让用这个目录」翻成人话：指出路径，并说清不是题目/模型的错。"""
    lines = [
        f"任务工作区不可用：{path}",
        "当前运行环境限制了目录权限：受限沙箱里，目录建出来就无法遍历或写入，"
        "而且事后再抬权限也会被拒。",
        "任务工作区必须能遍历、能读写；被限制时 bench 准备不了题目。",
    ]
    failing = getattr(exc, "filename", None)
    if failing and str(failing) != str(path):
        lines.append(f"系统报错位置：{failing}")
    lines.append(f"系统报错：{exc}")
    lines.append("换个不受限的位置，或退出文件沙箱后再跑。")
    return "\n".join(lines)


def _failing_path(exc: PermissionError, fallback: Path) -> Path:
    """异常里自带的那条路径优先——它比我们猜的准。"""
    failing = getattr(exc, "filename", None)
    return Path(failing) if failing else fallback


@contextlib.contextmanager
def temporary_workspace(prefix: str = "spool-bench-") -> Iterator[Path]:
    """建任务工作区用的临时根目录，保证**建出来就能用**。

    这里踩过一个真实的坑：`tempfile` 建目录时写死 0o700，而在带文件沙箱的
    Windows 上，用 0o700 建出来的目录**当场**就不可访问——`os.scandir` 都是
    WinError 5，连事后 `os.chmod` 也一起被拒，只能**创建那一刻**抬 mode。
    `shutil.copytree` 的 `os.makedirs` 正是撞在这上面崩的。

    所以这里不裸用 `TemporaryDirectory`，而是走 `spoolkit.sandbox_dirs`：
    与 `scripts/pytest_sandbox_compat.py`（pytest tmpdir 的同一个坑）
    **同一招、同一份实现**，不给这个仓库留第二份补丁。

    清理也**不能用 `TemporaryDirectory`**：工作区里跑过 pytest 之后会留下它自己
    用 0o700 建的 `pytest-cache-files-*`，而 `ignore_cleanup_errors=True` 在本沙箱里
    挡不住——它的处理器会去 `chmod` 那个目录，`chmod` 同样被拒，于是任务全跑完了
    却在收尾时抛出来（实测）。`shutil.rmtree(..., ignore_errors=True)` 才是真的
    「忽略清理错误」：索引数据库的连接在整个进程里都开着（SQLite 在 Windows 上
    会锁住文件），硬要在退出时删掉只会把结果一起吞掉。删不掉的留给系统回收。

    这一层的 `except PermissionError` **刻意包住整段任务**，不只是建目录那一下：
    受限环境里权限拒绝可能发生在 prepare、工作区遍历、验收等任何一步，而它们
    都是同一件事——环境不让用这个工作区。要求是**人话**，不是裸 traceback，
    所以兜底放在最外面；真正的位置由异常自带的路径（`exc.filename`）指出。
    """
    try:
        root = sandbox_dirs.make_accessible_temp_dir(prefix)
    except PermissionError as exc:
        # 连目录都没建出来：只能报「打算建在哪儿」。
        raise WorkspaceUnavailableError(
            describe_workspace_error(
                exc, _failing_path(exc, Path(tempfile.gettempdir()) / f"{prefix}*")
            )
        ) from None
    try:
        yield root
    except PermissionError as exc:
        # 建出来之后才撞上权限：同一类问题，同一句人话。
        raise WorkspaceUnavailableError(
            describe_workspace_error(exc, _failing_path(exc, root))
        ) from None
    finally:
        shutil.rmtree(root, ignore_errors=True)


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
    # 环境阻塞：验收环境根本用不了（缺 pytest）。与「模型没做到」是两回事，
    # 混在一起统计就会得出「模型不行」这个相反结论——所以要单独计数。
    blocked: bool = False


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
    try:
        if not source.exists():
            workdir.mkdir(parents=True, exist_ok=True)
        else:
            shutil.copytree(source, workdir)
        _copy_acceptance(task, workdir)
    except PermissionError as exc:
        # 工作区用不了是环境问题，别让它以裸 traceback 的形式糊到人脸上。
        raise WorkspaceUnavailableError(
            describe_workspace_error(exc, workdir)
        ) from None


def _copy_acceptance(task: Task, workdir: Path) -> None:
    for name in task.acceptance_files:
        shutil.copy(task.home / "acceptance" / name, workdir / name)


# --- 验收环境：整批开跑前先确认选中的解释器跑得起来 -------------------------
#
# 一次真实的误判：spool 装在 uv tool 里时，`sys.executable` 是**那个环境**的
# 解释器，而它没有 pytest。原先验收一律拿 sys.executable 跑，于是每一道题的
# 验收都死于 `No module named pytest`，报告上呈现为「验收 0/50」——看起来像
# 模型不行，其实是验收环境不行。那一整轮 50 题回归就这么白烧了。
#
# 所以这里做两件事，缺一不可：
# 1. 解释器按「能用」的顺序选（任务工作区 .venv → 检出目录 .venv → 当前解释器）；
# 2. 整批开跑**之前**探一次 pytest 在不在，不在就别开跑，给一句人话加一个
#    确定的退出码。跑完再解释是次优的：模型那一轮的 token 已经花掉了。

EXIT_ENVIRONMENT_BLOCKED = 3

# 只认「pytest 本身缺失」这一种。**刻意不把任意 ImportError 都算成环境问题**：
# 被测代码 import 不到东西完全可能是模型改坏的，那种必须留在「验收失败」里——
# 放宽这条会把真失败洗成环境阻塞，比原来的误判更难发现。
MISSING_PYTEST_MARKERS = (
    "No module named pytest",
    "No module named 'pytest'",
    'No module named "pytest"',
    "验收环境缺 pytest",
)


@dataclass(frozen=True)
class AcceptanceEnvironment:
    """这批任务验收会用到的解释器，以及它们跑不跑得起来 pytest。

    `pytest_tasks` 是**任务**的属性（验收命令里写没写 pytest），
    `missing_pytest` 是**环境**的属性（那个解释器跑不跑得起来）。
    两者同时成立才叫阻塞：有些题只用 python，那种题不该被缺 pytest 拦住。
    """

    interpreters: tuple[str, ...]
    pytest_tasks: tuple[str, ...]
    missing_pytest: tuple[str, ...] = ()
    probe_output: str = ""

    @property
    def needs_pytest(self) -> bool:
        return bool(self.pytest_tasks)

    @property
    def blocked(self) -> bool:
        return self.needs_pytest and bool(self.missing_pytest)

    @property
    def interpreter(self) -> str:
        """写进说明里的那一个：缺 pytest 的优先——它才是问题所在。"""
        if self.missing_pytest:
            return self.missing_pytest[0]
        if self.interpreters:
            return self.interpreters[0]
        return sys.executable


def task_needs_pytest(task: Task) -> bool:
    """这道题要不要 pytest。

    命令是数据，判据只能看它写了什么：整条命令里出现 `pytest` 就算。两种写法
    （`["pytest", …]` 与 `["python", "-m", "pytest", …]`）都覆盖。
    """
    return any("pytest" in part for part in task.command)


def probe_pytest(interpreter: str) -> tuple[bool, str]:
    """这个解释器跑不跑得起来 pytest。返回（能跑吗，原始输出）。

    判据刻意选成**将要执行的那条形状**（`<解释器> -m pytest --version`），而不是
    `import pytest`：验收命令走的就是 `-m pytest`，用它探到的问题才是真正会遇到的
    那个问题（比如 pytest 装上了、自己 import 就崩）。
    """
    try:
        proc = subprocess.run(
            [interpreter, "-m", "pytest", "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode == 0, output[-600:]


def acceptance_roots(workdir: Path) -> list[Path]:
    """验收解释器的候选根，按优先级排：任务工作区 → spoolkit 检出目录。

    第一条是「题目自带的环境」，第二条是「跑 bench 的这个包的检出目录」——
    后者是 uv tool / pipx / 系统 Python 装出来的 spool 唯一的救兵：
    那种安装自带的解释器里通常没有测试依赖，而检出目录里通常有。
    """
    return [workdir, spoolkit_checkout_root()]


def acceptance_interpreter(workdir: Path) -> str:
    """这道题的验收该用哪个解释器。"""
    return choose_interpreter(acceptance_roots(workdir))


def check_acceptance_environment(tasks: list[Task]) -> AcceptanceEnvironment:
    """整批开跑前的自检：选中的解释器跑不跑得起来 pytest。

    探的是**每道题自己的候选根**（题目自带 `.venv` 时，那才是它验收要用的
    解释器），同一个解释器只探一次。
    """
    interpreters: list[str] = []
    for task in tasks:
        candidate = choose_interpreter(acceptance_roots(task.home / "project"))
        if candidate not in interpreters:
            interpreters.append(candidate)

    missing: list[str] = []
    probe_output = ""
    for interpreter in interpreters:
        ok, output = probe_pytest(interpreter)
        if not ok:
            missing.append(interpreter)
            probe_output = probe_output or output

    return AcceptanceEnvironment(
        interpreters=tuple(interpreters),
        pytest_tasks=tuple(task.name for task in tasks if task_needs_pytest(task)),
        missing_pytest=tuple(missing),
        probe_output=probe_output,
    )


def _launch_hint() -> str:
    """「用仓库自己的 .venv 启动」的具体写法：能算出路径就给路径。"""
    checkout = spoolkit_checkout_root()
    python = venv_python(checkout)
    if python is None:
        return f"{checkout} 里的 `.venv`（spoolkit 检出目录）"
    script = python.parent / ("spool.exe" if os.name == "nt" else "spool")
    return f'"{script}" bench …（{checkout} 下的 .venv）'


def describe_environment_problem(environment: AcceptanceEnvironment) -> str:
    """把「验收解释器跑不了 pytest」翻成人话。

    要点三样（措辞以后可以改，这三样不能说没就没）：**解释器路径**、**pytest**、
    以及**这不是模型的问题**。最后一句最要紧——原先报告长成「验收 0/50」，
    读的人第一个念头必然是「模型不行」。
    """
    interpreter = environment.interpreter
    example = "、".join(environment.pytest_tasks[:3]) or "（没有）"
    lines = [
        "验收环境缺 pytest：选中的解释器跑不了 pytest，验收命令起不来，"
        "整批没有开跑。",
        f"  解释器：{interpreter}",
        f"  依据：这批任务里有 {len(environment.pytest_tasks)} 道的验收命令要用 "
        f"pytest（{example}）；探测方式 `{interpreter} -m pytest --version` "
        "返回非零。",
    ]
    if environment.probe_output:
        lines.append(f"  探测输出：{environment.probe_output.splitlines()[-1]}")
    lines += [
        "这不是模型的问题，也不是任务集的问题——没有跑，也就没有「验收失败 N 道」"
        "这回事；换成能跑 pytest 的解释器就能继续。",
        "怎么办（任选一条）：",
        f"  1. 用仓库自己的 .venv 启动：{_launch_hint()}",
        f'  2. 给上面那个环境装 dev 依赖："{interpreter}" -m pip install '
        '-e ".[dev]"（或直接装 pytest）。',
    ]
    return "\n".join(lines)


def looks_like_missing_pytest(text: str) -> bool:
    """这段输出是「环境里没有 pytest」，不是「测试没过」。

    两个地方用它：`verify` 把原始输出翻成人话，`run_task` / `verify_together`
    据此把这道题标成**环境阻塞**。不这么分，它就会混进「验收失败」明细里，
    而那正是误判的来源。
    """
    return any(marker in text for marker in MISSING_PYTEST_MARKERS)


def describe_missing_pytest(interpreter: str, output: str) -> str:
    """验收输出里出现 `No module named pytest` 时给人的那句话。"""
    return (
        "验收环境缺 pytest：验收命令用的解释器里没有它，测试根本没跑起来。\n"
        f"  解释器：{interpreter}\n"
        "这不是模型的问题，也不是这道题的问题——把它算进「验收失败」会得出"
        "相反的结论（spool 装在 uv tool / pipx / 系统 Python 里时最容易这样）。\n"
        "  出路：用仓库自己的 .venv 启动 spool（见 docs/benchmarking.md），"
        '或给上面那个解释器装 dev 依赖（`-m pip install -e ".[dev]"`）。\n'
        f"原始输出：\n{output[-600:]}"
    )



def verify(task: Task, workdir: Path) -> tuple[bool, str]:
    """用原始验收测试覆盖工作区里的同名文件，再跑验收命令。只看退出码。

    覆盖这一步是关键：agent 可能为了让它通过而改测试，覆盖之后那些改动
    一律作废，验收结果只反映它对被测代码做了什么。
    """
    _copy_acceptance(task, workdir)

    if not task.command:
        return False, "任务没有声明验收命令"

    # 命令里的 python / pytest 指向**能用的**那个解释器，而不是「跑 spool 的那个」：
    # 后者在 uv tool / pipx / 系统 Python 里通常没有 pytest。顺序见本段开头。
    roots = acceptance_roots(workdir)
    interpreter = choose_interpreter(roots)
    command = resolve_python_argv(list(task.command), roots)

    environment = dict(os.environ)
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
    if proc.returncode != 0 and looks_like_missing_pytest(output):
        # 这不是「这道题没过」。原样塞进失败明细，读的人只会去怀疑模型。
        return False, describe_missing_pytest(interpreter, output)
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
    # 工作区根目录必须是「建出来就能用」的那种，见 temporary_workspace。
    with temporary_workspace() as temp:
        workspace = workdir or temp / "project"
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
            # 验收环境用不了 ≠ 模型没做到。分类在这里做一次，报告那边才好分开计数。
            blocked=(not passed) and looks_like_missing_pytest(detail),
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
        # 环境阻塞单列一档：它不是「失败」，写「失败」等于把环境问题记在模型头上。
        outcome = "阻塞" if item.blocked else ("通过" if item.passed else "失败")
        lines.append(
            f"{item.name:<24}{outcome:<6}"
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
    # 阻塞的那些没有真的被验收过，所以必须在分母旁边点名，否则这个百分比会被
    # 当成「模型能力」。整批开跑前的自检能挡住绝大多数这种情况，这是第二道闸门。
    blocked = [item for item in results if item.blocked]
    if blocked:
        lines.append(
            f"环境阻塞 {len(blocked)} 个（验收环境缺 pytest 之类，"
            "不是模型的问题）：" + "、".join(item.name for item in blocked)
        )
    lines.append(f"自称完成 {claimed}/{total}")
    lines.append(f"平均步数 {average_steps:.1f}，平均输入 {average_tokens:.0f} token")

    # 「自称完成但验收失败」是最值得盯的一类：模型以为自己做到了，
    # 而客观信号说没有。这个数字高，说明验证机制没起作用。
    # 阻塞的不算：它的验收根本没跑起来，凭这个说模型「虚假完成」是冤枉。
    overconfident = [
        item
        for item in results
        if item.agent_finished and not item.passed and not item.blocked
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
                blocked=(not passed) and looks_like_missing_pytest(detail),
            )
        )
    return results


def render_together(results: list[TaskResult], meta: dict) -> str:
    passed = [item for item in results if item.passed]
    failed = [item for item in results if not item.passed and not item.blocked]
    blocked = [item for item in results if item.blocked]
    lines = [
        f"整批一条会话：验收通过 {len(passed)}/{len(results)}",
        f"这条会话：{meta['steps']} 步 / {meta['calls']} 次调用 / "
        # 输出 token 也要记：光有输入看不出「时间花在生成上还是工具上」，
        # 而这两者的优化方向完全不同。
        f"{meta['prompt_tokens']} 输入 + {meta.get('completion_tokens', 0)} 输出 token / "
        f"{meta['seconds']}s",
        f"工作区留在 {meta['workspace']}（可以进去看它实际改成了什么样）",
    ]
    if blocked:
        # 与「没过的」分开说：这一类没有真的被验收过，写在一起就会被读成模型不行。
        lines.append(
            f"环境阻塞 {len(blocked)} 个（验收环境缺 pytest 之类，"
            "不是模型的问题）：" + "、".join(item.name for item in blocked)
        )
    if failed:
        lines.append("没过的：" + "、".join(item.name for item in failed))
    return "\n".join(lines)
