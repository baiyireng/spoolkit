"""自动审查层 P2：L2 行为回归（设计稿 §5 L2 的三条判据）。

L1（`spoolkit.agent.audit`）判的是**静态一致性**：残留引用、引用闭合、测试被
改弱。它有一条抓不到的失败形状，这一层就是为它写的：

    `vague-optimize` 在 t=0.7 那一次把 `path_counts` 改成 `Counter`，却把
    `if path not in counts: order.append(...)` 写在自增**之后** →
    `top_paths=[]`，耗时 0.04s「变快了」而**结果坏了**，而自动验证 2/2 绿、
    最终验证绿。**静态看一切自洽，只有把改动前后跑一遍、逐字比对输出才看得见。**

三条判据各自机械、各自可证伪：

| 编号 | 判什么 | 判据一句话 | 处置 |
| --- | --- | --- | --- |
| L2.2 | **对照运行** | 用 `Baseline`（改动前的正文）在试跑副本里复原「改动前」的树，与「改动后」的树跑**同一条入口命令、同一份输入**，逐字比对 stdout/stderr/退出码 | 差异是**行为变化发现**（不是自动判失败）；任务没要求改行为时升级为高严重度 |
| L2.3 | 接口快照 | 公开符号（`EscalationGate.public_symbols`，P1 已接）+ CLI `--help` 文本，改动前后比对 | 意外的删除/改名/签名变化 → 高严重度（T1 已在同一批路径上接过一次，这里补 `--help` 那一半） |
| L2.4 | 性能不倒退 | 任务属于「优化/提速」类时，要有**实测改善**且**行为未变**；行为变了就不算通过 | 记实测耗时；变慢或没改善 → 高严重度 |

**拿不到对照时明说「查不了」**（沿用 `tools/verify.CANNOT_CHECK` 的口径，
不另造一套字面量、**不静默通过**）：项目没有可跑的入口、README 里没有能用
的入口命令、找不到输入样本、入口跑不起来——每一种都给出具体理由。

处置口径（§3.2.0 / §2.5）：发现**只拦「完成声明」与危险动作，不拦成果**。
成果照落盘、可回退、收尾报告逐条列出；**删死代码、合并重复实现、删已被替代
的文件都是正常编辑**，这一层判的从来不是「删了东西」，而是「同一条命令、
同一份输入、输出不一样了」。
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from spoolkit import sandbox_dirs
from spoolkit.agent.audit import HIGH, INFO, Finding
from spoolkit.tools.edit import PendingChange
from spoolkit.tools.trial import copy_project

# 「查不了」的既有字面量（`tools/verify.CANNOT_CHECK`）。这里**不另造一套**：
# 同一件事在两个地方用两个词，读日志的人就得自己对齐。
CANNOT_CHECK = "【查不了】"

#: L2.2 判据名。探针按它数 `review_findings_by_kind`。
BEHAVIOR_CHANGED_KIND = "behavior_changed"
#: L2.3 判据名（CLI `--help` 那一半；公开符号那一半由 T1 报）。
CLI_SURFACE_CHANGED_KIND = "cli_surface_changed"
#: L2.4 判据名。
PERFORMANCE_REGRESSED_KIND = "performance_regressed"
#: 「这一步没跑成」也用 L2.2 的 kind：它是**读数**，不是发现（severity=INFO）。
BEHAVIOR_NOT_CHECKED_KIND = "behavior_not_checked"
#: L2 的**过程读数**（判据原话 / 实际跑的命令 / 逐字比对的结果 / 耗时 /
#: 「查不了」的理由）：它进事件流与 trace，供人跟踪与排查；**不是发现**
#: （探针不按它数 `review_findings`），也**不进结论报告**——报告里出现内部
#: 读数时，§2.5 的反向条款会跟着一起出现，于是"什么都没拦下"的那一轮里
#: 也读得到一段无关样板（问题③在 risky-action 那一轮的形状）。
BEHAVIOR_READINGS_KIND = "behavior_readings"

#: 只比这一个文件名的入口说明（README 是通用约定，不新造格式）。
README_NAMES = ("README.md", "readme.md", "README.rst")

#: 入口命令的识别形状：一段 `python(x) <脚本> [输入...]`。
_ENTRY_COMMAND = re.compile(r"^python3?\s+(\S+\.py)\s+(\S+)\s*$")
#: 模块入口的形状：`python(x) -m <模块> <输入文件>`（包里带 `__main__` / CLI 的
#: 项目都这么写，例如 `python -m pipeline.cli data/parts.csv`）。判据与脚本那条
#: 一样严：**必须点名一个项目里真的存在的文件**，否则不认（只有开关的用法
#: 没有「同一份输入」可比）。
_MODULE_COMMAND = re.compile(r"^python3?\s+-m\s+([A-Za-z_][\w.]*)\s+(\S+)\s*$")
#: 生成输入的形状：`python(x) <脚本> <参数…> > <文件>`。README 里写「本地复现
#: 要用」的那一条就是它——它给的是**和线上同量级**的输入，而示例样本常常只是
#: 随手留的一小段。对照运行优先用它：小样本上「优化」这件事根本量不出来。
_GENERATE_COMMAND = re.compile(r"^python3?\s+(\S+\.py)\s+([^>]*?)\s*>\s*(\S+)\s*$")
#: README 里的围栏代码块（``` 与 ~~~ 都认）。
_FENCE = re.compile(r"^\s*(```|~~~)")

#: 输入样本的候选目录（找不到 README 点名的那一份时按它找）。
INPUT_DIRS = ("data", "fixtures", "samples", "inputs", "testdata")
#: 输入样本至少要有这么大：太小的样本测不出「少读一半」这类行为差异。
MIN_INPUT_BYTES = 512
#: 一次对照运行的墙钟上限（秒）。走标定值 `behavior_entry_timeout`，这是兜底。
DEFAULT_TIMEOUT = 60.0

#: L2.4 的标定：基线耗时低于这个数就不判「快不快」（计时噪声比差值大）。
#: 0.30s 是「起一个 Python 进程 + 导入模块」的固定开销量级——低于它，
#: 相对差值大半是噪声，拿它判「快了多少」等于报一个不成立的数字。
PERF_FLOOR_SECONDS = 0.30
#: 实测**改善**的判定线：改动后必须比改动前快 15% 以上才算「有改善」。
PERF_IMPROVE_RATIO = 0.85
#: 实测**倒退**的判定线：改动后必须比改动前慢 25% 以上才算倒退。
#:
#: 为什么改善与倒退两条线**不对称、中间留一段不判**：同一份代码跑两次的实测
#: 抖动实测在 ±10% 上下（冒烟里「不改」那一档 10.1s → 9.4s、9.3s → 9.1s、
#: 10.5s → 13.4s），单条线会把噪声判成「倒退」——而 L2.4 是**高严重度**，
#: 一次误报就会去拦一次正确的完成声明（§4：L1/L2 有误报就是判据写错了）。
#: 所以倒退要比噪声带更宽，而「没改善」本身不是缺陷（任务是优化，但没快到
#: 越过 15% 时只如实记读数）。
PERF_REGRESS_RATIO = 1.25


@dataclass(frozen=True)
class RunResult:
    """一次入口运行的读数。"""

    code: int
    stdout: str
    stderr: str
    seconds: float = 0.0
    #: "" = 跑完了；"timeout" / "spawn_error" = 没跑成（**不是**行为差异）。
    status: str = ""

    @property
    def ran(self) -> bool:
        return self.status == ""


#: 跑一条入口命令的那一层。签名与 `_spawn` 一致（`stdout_to` 是 README 里
#: 那条命令自带的重定向落点）；用例注入假 runner 就换掉这一层，判据不动。
Runner = Callable[..., RunResult]


@dataclass(frozen=True)
class EntryPoint:
    """一次对照运行的入口：跑哪条命令、拿哪份输入、为什么是它。"""

    command: tuple[str, ...]
    input_path: str
    source: str
    #: README 里那条「怎么造出线上那份输入」的命令（没有就为空）。
    #: `("gen_log.py", "40000", "data/big.log")` 的形状：脚本、参数、落点。
    generator: tuple[str, ...] = ()
    #: 入口命令自带的重定向落点（`python gen.py 10 > out.txt` 的 `out.txt`）。
    #: 绝大多数项目没有它；有它时输出被写进文件，比对的就是那个文件。
    stdout_redirect: str = ""
    #: `generator` 里哪个位置是「规模」参数（-1 = 没有）：标定时只动它。
    scale_index: int = -1

    @property
    def declared_units(self) -> int:
        """README 那条造输入命令里声明的规模（没有就是 0）。"""
        args = self.generator[1:-1]
        if 0 <= self.scale_index < len(args):
            item = args[self.scale_index]
            if item.isdigit():
                return int(item)
        return 0

    @property
    def self_generating(self) -> bool:
        """入口命令本身就是造输入那条（`python gen.py 10 > out.txt`）。

        这种项目没有「读输入、出结果」的入口可比，能机械比的就是**同一个
        确定性生成器前后产出是否逐字一致**——生成器被改坏会被报出来，
        而「我们查不了」不该由它冒充。
        """
        return bool(self.stdout_redirect and self.generator)


@dataclass
class BehaviorReport:
    """L2 的完整读数。**它只报事实**，判成什么由 `findings` 决定。"""

    findings: list[Finding] = field(default_factory=list)
    #: 「查不了」的具体理由（空 = 跑成了）。
    cannot_check: str = ""
    command: str = ""
    input_path: str = ""
    before: str = ""
    after: str = ""
    #: 逐字比对的结果：True = 输出一字未变。
    same: bool | None = None
    changed: bool = False
    #: L2.4 的实测耗时（秒）。0.0 = 没测到。
    before_seconds: float = 0.0
    after_seconds: float = 0.0
    #: 优化类任务是否拿到了「有改善且行为未变」的结论（None = 判不了）。
    faster: bool | None = None
    optimization: bool = False
    #: L2.3：公开符号快照里被删/改签名的那些（由调用方从 `EscalationGate` 取）。
    interface_changes: list[str] = field(default_factory=list)
    #: L2.3：`--help` 文本比对的结论（`None` = 没跑）。
    cli_checked: bool | None = None
    cli_files: tuple[str, ...] = ()
    #: 生成器规模向下标定过时的一句说明（空 = 按 README 原样）。
    scale_note: str = ""

    @property
    def checked(self) -> bool:
        return self.same is not None

    def render(self) -> str:
        """一段人读的读数（收尾报告与事件流共用）。"""
        if not self.checked:
            why = self.cannot_check or "没有可跑的对照"
            return f"{CANNOT_CHECK}行为回归（L2.2）：{why}"
        verdict = "逐字相同" if self.same else "**逐字不一致**"
        timing = ""
        if self.before_seconds and self.after_seconds:
            timing = (
                f"；耗时 改动前 {self.before_seconds:.3f}s / "
                f"改动后 {self.after_seconds:.3f}s"
            )
        return (
            f"L2.2 对照运行：{self.command}（输入 {self.input_path}）"
            f"输出{verdict}{timing}"
        )


# ---- 入口解析（「跑哪条命令、拿什么输入」）-----------------------------------


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _code_blocks(text: str) -> list[list[str]]:
    """README 里的围栏代码块（去掉围栏行与空行）。"""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if _FENCE.match(line):
            if current is None:
                current = []
            else:
                blocks.append(current)
                current = None
            continue
        if current is not None:
            stripped = line.strip()
            if stripped:
                current.append(stripped)
    if current:
        blocks.append(current)
    return blocks


def _under_root(root: Path, stored: str) -> bool:
    try:
        (root / stored).resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def _existing_input(root: Path, name: str) -> tuple[str, int] | None:
    """README 点名的输入文件在不在；在就返回（相对路径, 字节数）。"""
    if name.startswith("-") or not _under_root(root, name):
        return None
    target = root / name
    try:
        if not target.is_file():
            return None
        return (Path(name).as_posix(), target.stat().st_size)
    except OSError:
        return None


def _readme_lines(root: Path) -> list[str]:
    for name in README_NAMES:
        text = _read_text(root / name)
        if not text:
            continue
        return [line for block in _code_blocks(text) for line in block]
    return []


def entry_point(root: Path) -> EntryPoint | None:
    """机械地找一次「能跑的对照」：README 里的命令 + 它点名的输入。

    为什么以 README 为准（而不是自己编一条命令）：README 里的用法示例是
    **项目自己声明的入口**，改动前后都在同一份 README 上解析，命令与输入
    一字不改——这正是 L2.2 要的「同输入」。README 换过、命令解析不出来时
    明说「查不了」，不猜一条命令去跑（猜出来的命令跑出来的差异没有意义）。

    **优先用「造输入」那条命令**（`python gen_log.py 40000 > data/big.log`）：
    它给的是和线上同量级的输入，而示例样本常常只是随手留的一小段。两件事都
    靠它：对照运行的**判别力**（小样本上有些行为差异根本不触发）与 L2.4 的
    **可测性**（跑 0.1s 的程序量不出「快了没有」，而优化类任务要拿实测改善
    说话）。生成器是确定性的（种子写在项目自己的脚本里），所以两边生成的
    输入一模一样——这仍然是「同一份输入」。

    没有生成器时退回「用法示例 + 它点名的那份输入」：同一份 README 里有多条
    时取**体积最小**的——小样本只是判别力弱一些，而它是项目自己留的样本。
    用法示例认得两种写法：`python <脚本> <输入>` 与 `python -m <模块> <输入>`
    （包里带 CLI 的项目都这么写，例如 `python -m pipeline.cli data/parts.csv`）。
    两种都要求**点名一个项目里真的存在的文件**：只有开关的用法（
    `python -m expenses.cli total`）没有「同一份输入」可比，不认。

    **生成器的规模会向下标定一档**（见 `calibrate_scale`）：README 那条命令
    的规模是「够用就行」，而 O(n²) 的老实现在那个规模上可能**跑不完**
    （实测 `vague-optimize` 的 40000 行上改动前那棵树要几十秒），于是对照
    整体变成「查不了」。向下标定到「改动前能在预算内跑完」的规模，
    两边生成的是同一份确定性输入，读数才开始有意义。
    """
    examples: list[tuple[int, tuple[str, ...], str]] = []
    generator: tuple[tuple[str, ...], str] | None = None
    scale_index = -1
    for line in _readme_lines(root):
        gen = _GENERATE_COMMAND.match(line)
        if gen is not None and generator is None:
            script, args, target = gen.group(1), gen.group(2), gen.group(3)
            if (root / script).is_file() and _under_root(root, target):
                parts = [item for item in args.split() if item]
                scale_index = next(
                    (index for index, item in enumerate(parts) if item.isdigit()), -1
                )
                generator = ((script, *parts, target), target)
                continue
        module = _MODULE_COMMAND.match(line)
        if module is not None:
            name, sample = module.group(1), module.group(2)
            found = _existing_input(root, sample)
            if found is None:
                continue
            stored, size = found
            examples.append(
                (size, ("python", "-m", name, stored), stored)
            )
            continue
        hit = _ENTRY_COMMAND.match(line)
        if hit is None:
            continue
        script, sample = hit.group(1), hit.group(2)
        if not (root / script).is_file():
            continue
        found = _existing_input(root, sample)
        if found is None:
            continue
        stored, size = found
        examples.append((size, ("python", script, stored), stored))
    if generator is not None:
        raw, target = generator
        script, args = raw[0], list(raw[1:-1])
        generated = (
            " ".join((script, *args, ">", target)) if args else f"{script} > {target}"
        )
        if examples:
            # **要用「消费输入」那条命令跑，输入换成生成出来的那一份**：
            # 拿生成器本身当入口是错的——`python gen_log.py data/big.log`
            # 根本不读日志（它是造数据的脚本），两次输出当然一样，
            # 那是**假的通过**。入口永远是「读输入、出结果」那一条。
            _, example, _ = min(examples, key=lambda item: item[0])
            command = (example[0], example[1], target)
            return EntryPoint(
                command=command,
                input_path=target,
                source=(
                    "README 的用法示例 + 「造输入」命令（"
                    f"python {generated}）：输入与线上同量级"
                ),
                generator=(script, *args, target),
                scale_index=scale_index,
            )
        # 只有造数据那一条命令：对照的判据就是「同一段确定性输入生成器，
        # 前后两棵树产出逐字一致」（这一条仍然有判别力：生成器被改坏就报）。
        return EntryPoint(
            command=("python", script, *args),
            input_path=target,
            source=f"README 的「造输入」命令（python {generated}）",
            generator=(script, *args, target),
            stdout_redirect=target,
            scale_index=scale_index,
        )
    if not examples:
        return None
    size, command, stored = min(examples, key=lambda item: item[0])
    return EntryPoint(
        command=command,
        input_path=stored,
        source=f"README 的用法示例（样本 {size} 字节，取最小的那份）",
    )


#: 标定的绝对下限：规模再小就测不出「少读一半」这类差异了。
SCALE_MIN_UNITS = 2000
#: 标定试跑的墙钟上限（秒）：标定只回答「改动前跑不跑得完」，不该按整条对照的
#: 预算等——超时就换小一档，等满 60 秒只是为了拿一个「不行」的结论。
CALIBRATION_TIMEOUT = 20.0
#: 标定只考虑这几档（原值 × 比例）。一半的台阶够了：README 的规模是项目自己
#: 声明的，我们只是把它降到「老实现跑得完」的量级（实测 40000 → 20000 时，
#: 老实现从「几十秒跑不完」变成 9.7s，而修好版是 0.10s）。
SCALE_RATIOS = (1.0, 0.5)


def _scaled_args(entry: EntryPoint, units: int) -> list[str]:
    """把规模参数换成 `units` 之后的参数列表（其余参数一字不动）。"""
    out = list(entry.generator[1:-1])
    if 0 <= entry.scale_index < len(out):
        out[entry.scale_index] = str(units)
    return out


def _scale_candidates(entry: EntryPoint) -> list[int]:
    """标定的候选规模（原值 + 一半），下限 `SCALE_MIN_UNITS`。"""
    base = entry.declared_units
    if base <= 0:
        return []
    out: list[int] = []
    for ratio in SCALE_RATIOS:
        units = max(SCALE_MIN_UNITS, int(base * ratio))
        if units not in out:
            out.append(units)
    return out


def _freeze_generator(entry: EntryPoint, changes: Sequence[PendingChange]) -> str:
    """L2.2 的「同一份输入」怎么保住：**生成器取改动前那一版**。

    实测抓出来的（探针 set5 的 `vague-optimize-r1`）：模型顺手把 `gen_log.py`
    也改了（它也在「让 logstat 变快」的射程里），于是两边**各生成了一份不同的
    输入**——那不再是「同一条命令、同一份输入」，比出来的差异说明不了行为变了
    没有（那一次 L2 报的是「改动后退出码 1」，而改动后那棵树的入口在探针自己
    的输入上是好的）。这条判据只能靠「输入由同一段代码产生」成立。

    做法：`Baseline` 里的 `old_text` 就是生成器改动前的正文（不另造原料），
    把它写回**两份副本**——两边用同一个生成器造输入，被测的仍然是被改过的那
    份代码。返回生成器相对路径（空串 = 没改过它）。
    """
    if not entry.generator:
        return ""
    script = entry.generator[0]
    for change in changes:
        if Path(change.path).as_posix() == Path(script).as_posix():
            return change.path
    return ""


def _pin_generator(root: Path, changes: Sequence[PendingChange], stored: str) -> None:
    """把生成器钉回改动前的正文（只改这一份副本，工作区一动不动）。"""
    for change in changes:
        if change.path != stored:
            continue
        target = root / stored
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(change.old_text, encoding="utf-8")
        return


def calibrate_scale(
    root: Path,
    entry: EntryPoint,
    timeout: float,
    *,
    runner: Runner | None = None,
) -> tuple[int, str, str]:
    """在**改动前**那棵树上把生成器规模向下标定到「跑得完」。

    返回 `(规模, 造输入时不能跑的原因, 说明)`；规模为 0 表示按 README 原样。

    三条规矩：

    1. **量的是入口命令，不是生成器**：生成器永远很快（造数据而已），
       跑不完的是「读这份输入」的那个程序。第一版拿生成器的耗时当判据，
       于是「40000 行生成得很快 → 原样使用 → 入口 60 秒超时」——整条对照
       退化成「查不了」，而这正是要避免的那种静默失效。
    2. **只在改动前那棵树上试、只向下**：对照的公平性靠「两边最终用同一个
       规模」保证；向上放大等于替项目编一个基准（80000 行上老实现直接跑不完）。
    3. **试跑用的输入留在副本里**：选定规模之后，`_prepare_input` 不重新生成
       （文件已经在那里），省一次生成。
    """
    candidates = _scale_candidates(entry)
    if not candidates:
        return (0, "", "")
    base = entry.declared_units
    limit = min(float(timeout), CALIBRATION_TIMEOUT)
    run = runner or _spawn
    smallest = candidates[-1]
    for units in candidates:
        why = _prepare_input(root, entry, timeout, units=units)
        if why:
            return (0, why, "")
        done = run(root, entry.command, limit, stdout_to=entry.stdout_redirect)
        if not done.ran:
            # 这一档在**短预算**里没跑完：换小一档。
            #
            # 实测教训（探针 set5 的 vague-optimize-r0）：标定的短预算（20s）被
            # 机器负载顶掉时，最后只剩「按 README 的 40000 原样跑」这一条路，
            # 而那一条必然超时——整条对照退化成「查不了」，最有价值的那个探针
            # 恰好测不到。所以最小那一档再用**整条对照的预算**试一次：慢不等于
            # 不能比，只要它在预算内跑得完，读数就有效。
            if units != smallest:
                continue
            done = run(root, entry.command, timeout, stdout_to=entry.stdout_redirect)
            if not done.ran:
                continue
        if units == base:
            return (units, "", "")
        return (
            units,
            "",
            f"README 声明的规模 {base} 在改动前那棵树上跑不完，"
            f"向下标定到 {units}（两边同一份确定性输入）",
        )
    # 连最小那一档都跑不完：不标定，让上层按「查不了」如实报出来。
    return (0, "", "")


def _input_ready(root: Path, entry: EntryPoint, units: int) -> bool:
    """这一份副本里的输入是不是已经造好了（标定留下的那一份）。"""
    if not entry.generator or entry.self_generating:
        return True
    return (root / entry.generator[-1]).is_file()


def _prepare_input(
    root: Path,
    entry: EntryPoint,
    timeout: float,
    *,
    units: int = 0,
) -> str:
    """在**这一份副本里**按 README 把输入造出来。返回不能造的原因（空 = 造好了）。"""
    if not entry.generator or entry.self_generating:
        # 自造自比的那一类：入口命令自己就带重定向，不需要预先造输入。
        return ""
    script, target = entry.generator[0], entry.generator[-1]
    args = _scaled_args(entry, units) if units else list(entry.generator[1:-1])
    argv = ["python", script, *args]
    done = _spawn(root, argv, timeout, stdout_to=target)
    if not done.ran:
        return f"造输入没跑成（{' '.join(argv)}：{done.status or done.stderr[:120]}）"
    if not (root / target).is_file():
        return f"造输入没产出 {target}（{' '.join(argv)}）"
    return ""


# ---- 改“前”的那棵树（复用 Baseline，不另造）--------------------------------


def _baseline_changes(changes: Sequence[PendingChange]) -> list[PendingChange]:
    """把待确认改动翻成「改动前」：`new_text=old_text`。

    走的是既有的 `Baseline`（`edit.baseline()` 产出的那份）与
    `trial_workspace` 的同一套材料化路径——**不另造一套「改动前」的树**。
    新建的文件在「改动前」不存在，所以它在这里被写空（`deleted=True`）。
    """
    out: list[PendingChange] = []
    for change in changes:
        if change.is_new_file:
            out.append(
                PendingChange(path=change.path, old_text="", new_text="", deleted=True)
            )
        else:
            out.append(
                PendingChange(
                    path=change.path,
                    old_text=change.old_text,
                    new_text=change.old_text,
                )
            )
    return out


class _Trial:
    """一份用完即删的项目副本（抬过 mode，见 `spoolkit.sandbox_dirs`）。"""

    def __init__(self, root: Path, changes: Sequence[PendingChange], tag: str) -> None:
        self.base = sandbox_dirs.make_accessible_temp_dir(f"spool-{tag}-")
        self.path = self.base / "project"
        copy_project(root, self.path)
        for change in changes:
            if Path(change.path).is_absolute():
                # 关联目录的改动不进副本：副本只代表主工作区。
                continue
            target = self.path / change.path
            if getattr(change, "deleted", False):
                if target.exists():
                    target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(change.new_text, encoding="utf-8")

    def close(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)

    def __enter__(self) -> Path:
        return self.path

    def __exit__(self, *exc: object) -> None:
        self.close()


# ---- 跑一条命令（同一条入口，两边各跑一次）---------------------------------


def _spawn(
    root: Path,
    command: Sequence[str],
    timeout: float,
    *,
    stdout_to: str = "",
) -> RunResult:
    """在 `root` 里跑一条命令，收 stdout/stderr/退出码/耗时。

    刻意**不走** `tools/exec.run_once`：那一条会再套一层试跑副本（我们要的
    副本已经在这里了），而且它按模型工具的语义解释命令。这里只是「跑同一条
    命令两次，比输出」，命令来自项目 README、不是模型写的。

    `stdout_to` 是 README 里那条命令自带的 `> <文件>` 重定向（造输入用）：
    它必须落在**这一份副本**里，所以不经过 shell，由这里翻译成 `stdout=文件`。
    """
    argv = list(command)
    if argv and argv[0] in ("python", "python3"):
        argv[0] = sys.executable
    handle = None
    if stdout_to:
        target = root / stdout_to
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = target.open("w", encoding="utf-8", newline="\n")
    started = time.perf_counter()
    try:
        done = subprocess.run(
            argv,
            cwd=str(root),
            stdout=handle if handle is not None else subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1.0, float(timeout)),
        )
    except subprocess.TimeoutExpired:
        return RunResult(code=-1, stdout="", stderr="", status="timeout")
    except OSError as exc:  # 入口跑不起来（解释器/权限/路径）
        return RunResult(code=-1, stdout="", stderr=str(exc), status="spawn_error")
    finally:
        if handle is not None:
            handle.close()
    return RunResult(
        code=done.returncode,
        stdout=done.stdout or "",
        stderr=done.stderr or "",
        seconds=time.perf_counter() - started,
    )


def _normalize(text: str, roots: Sequence[Path]) -> str:
    """把副本的随机目录名抹平——它每次都不同，不抹就会把所有运行都报成差异。

    这是**噪声控制**，不是放宽判据：抹掉的只有「这一次跑在哪个临时目录里」，
    输出内容一个字符都不动。
    """
    for root in roots:
        for form in (str(root), str(root).replace("\\", "/")):
            text = text.replace(form, "<工作副本>")
    return text


def _digest(result: RunResult, roots: Sequence[Path]) -> str:
    body = "\n".join(
        (
            f"exit={result.code}",
            _normalize(result.stdout, roots),
            _normalize(result.stderr, roots),
        )
    )
    return hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()


def _excerpt(text: str, limit: int = 200) -> str:
    clean = " ".join(text.split())
    return clean[:limit]


# ---- 任务是不是「优化/提速」类（L2.4 的触发条件）----------------------------


def is_optimization_task(goal: str) -> bool:
    """这句话要的是「更快」，还是「改行为」。

    机械判据：请求里出现提速类词（优化/提速/更快/太慢/性能/加速…）**并且**
    没有出现「改行为」类词（新增/加一个/改成…）时算优化类。判错了的代价是
    L2.4 多判或少判一条读数，**不改变** L2.2 的行为比对（那条永远跑）。
    """
    text = str(goal or "")
    if not text:
        return False
    speed = (
        "优化",
        "提速",
        "加速",
        "更快",
        "快一点",
        "快点",
        "弄快",
        "太慢",
        "很慢",
        "慢得",
        "卡半天",
        "性能",
        "耗时",
        "跑得慢",
    )
    change = ("新增", "加一个", "添加", "改成", "改名", "重命名", "删掉", "支持")
    return any(word in text for word in speed) and not any(
        word in text for word in change
    )


# ---- 主判据 ------------------------------------------------------------------


def review(
    root: Path,
    changes: Sequence[PendingChange],
    *,
    goal: str = "",
    interface_changes: Sequence[str] = (),
    timeout: float = DEFAULT_TIMEOUT,
    runner: Runner | None = None,
) -> BehaviorReport:
    """L2 的入口：对照运行 + 接口快照 + 性能不倒退。

    `changes` 是待确认清单里的那一批（`PendingChange`）。清单为空时返回一份
    「没跑」的读数（这一轮没改过东西，没有「改动前后」可比）。
    `interface_changes` 是公开符号快照的差异（调用方从 `EscalationGate` 取），
    这里只把它记进读数：**报与拦都走 T1 那条既有通道**，不重复升级。
    """
    report = BehaviorReport(
        optimization=is_optimization_task(goal),
        interface_changes=list(interface_changes),
    )
    items = [item for item in changes if not Path(item.path).is_absolute()]
    if not items:
        report.cannot_check = "这一轮没有可比的改动（待确认清单里没有工作区内的改动）"
        return report
    entry = entry_point(root)
    if entry is None:
        # 这一句会原样进收尾报告（`BehaviorReport.render()` 给它加上判据名那
        # 个前缀），所以它是**一行**、有上限：对一次只写了个小文件的运行来说，
        # 铺一整段「查不了」就是无关样板（问题⑥）。理由本身一个字不少。
        report.cannot_check = (
            "找不到可跑的对照（README 里没有能跑出同一份输入的用法示例）"
        )
        return report
    report.command = " ".join(entry.command)
    report.input_path = entry.input_path

    after_changes = list(items)
    before_changes = _baseline_changes(items)
    # 「同一份输入」的保障：生成器若被改过，两份副本都用**改动前**那一版
    # （见 `_freeze_generator`）。它必须在造输入之前落地，所以放在副本上下文里。
    frozen = _freeze_generator(entry, items)
    run = runner or _spawn
    try:
        with _Trial(root, before_changes, "behavior-before") as before_root:
            with _Trial(root, after_changes, "behavior-after") as after_root:
                if frozen:
                    _pin_generator(before_root, before_changes, frozen)
                    _pin_generator(after_root, after_changes, frozen)
                roots = (before_root, after_root)
                # 生成器的规模只在**优化类任务**上向下标定：那一类要拿实测
                # 耗时说话，README 写的规模上老实现可能根本跑不完；其余任务
                # 只比「输出一不一样」，按声明规模跑即可（省一次试跑）。
                #
                # 标定留下的输入**直接复用**（`_input_ready`）：同一份副本里
                # 再生成一遍只是白花时间。
                units = 0
                if report.optimization:
                    units, why, note = calibrate_scale(
                        before_root, entry, timeout, runner=runner
                    )
                    if why:
                        report.cannot_check = f"对照运行的前置步骤没做成：{why}"
                        return report
                    # 只在**真的向下改过规模**时才写进报告：README 声明的规模
                    # 原样可用时，输入就是那一份，不必多一句。
                    if units and units != entry.declared_units:
                        report.scale_note = note
                        report.input_path = f"{entry.input_path}（规模 {units}）"
                # 造输入走的是**每一份副本自己**的 README 命令：两边的输入
                # 由同一段确定性代码生成、落在各自副本里，工作区一动不动。
                #
                # **两边都要跑**（不能用 `a or b` 短路）：第一份造成功之后
                # 短路会让第二份根本没有输入，于是「改动后」那一棵树跑的是
                # 不存在的大样本——两条读数都作废。
                if not _input_ready(before_root, entry, units):
                    why = _prepare_input(before_root, entry, timeout, units=units)
                    if why:
                        report.cannot_check = f"对照运行的前置步骤没做成：{why}"
                        return report
                if not _input_ready(after_root, entry, units):
                    why = _prepare_input(after_root, entry, timeout, units=units)
                    if why:
                        report.cannot_check = f"对照运行的前置步骤没做成：{why}"
                        return report
                run = runner or _spawn
                first = run(
                    before_root,
                    entry.command,
                    timeout,
                    stdout_to=entry.stdout_redirect,
                )
                second = run(
                    after_root,
                    entry.command,
                    timeout,
                    stdout_to=entry.stdout_redirect,
                )
                if entry.stdout_redirect:
                    # 自造自比那一类：输出写在文件里，比对的是那个文件。
                    first = _read_produced(before_root, entry, first)
                    second = _read_produced(after_root, entry, second)
                report.before_seconds = first.seconds
                report.after_seconds = second.seconds
                if not (first.ran and second.ran):
                    report.cannot_check = _run_failure_note(first, second)
                    return report
                report.before = _digest(first, roots)
                report.after = _digest(second, roots)
                same = report.before == report.after
                report.same = same
                report.changed = not same
                if not same:
                    report.findings.append(_changed_finding(report, first, second))
                report.findings.extend(_performance_findings(report))
    except OSError as exc:
        report.same = None
        report.cannot_check = f"试跑副本建不出来：{type(exc).__name__}: {exc}"
        return report
    # L2.3 的公开符号那一半：差异由调用方从 `EscalationGate` 取。这里只把它
    # 记进读数的**提示**档，不另发一条高严重度发现——T1 已经拿这同一批证据
    # 升级过一次（`completion_triggers` 的 `changed_public_symbols`），再发
    # 一条就是把同一处接口变化数成两条（探针按 note 数发现条数），而去重与
    # 升级出口都在 T1 那一条上。**改名本身是任务要求的，不是缺陷**：读数的
    # 价值在于「接口动了」这件事在报告里看得见。
    for name in report.interface_changes:
        report.findings.append(
            Finding(
                kind=CLI_SURFACE_CHANGED_KIND,
                severity=INFO,
                summary=f"公共接口被删或改签名：{name}",
                detail="公开符号快照改动前后比对（T1 的同一批证据；"
                "改动前那一份取自 `EscalationGate.snapshot_symbols`）",
                path=name.split(":", 1)[0],
            )
        )
    return report


def _run_failure_note(first: RunResult, second: RunResult) -> str:
    """入口没跑成时，**明说为什么**（不静默通过）。"""
    which = "改动前" if not first.ran else "改动后"
    result = first if not first.ran else second
    if result.status == "timeout":
        return (
            f"入口跑不完：{which}那一棵树在超时前没结束"
            "（超时不算行为差异，也不当通过）"
        )
    return f"入口跑不起来：{which}那一棵树报 {_excerpt(result.stderr) or '没有输出'}"


def _read_produced(root: Path, entry: EntryPoint, result: RunResult) -> RunResult:
    """自造自比那一类：入口的输出落在文件里，把它读成 stdout。"""
    if not result.ran:
        return result
    try:
        body = (root / entry.stdout_redirect).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return RunResult(
            code=result.code,
            stdout="",
            stderr=f"{result.stderr}\n读不出产出 {entry.stdout_redirect}: {exc}",
            seconds=result.seconds,
        )
    return RunResult(
        code=result.code, stdout=body, stderr=result.stderr, seconds=result.seconds
    )


def _changed_finding(
    report: BehaviorReport, first: RunResult, second: RunResult
) -> Finding:
    """L2.2：同一条命令、同一份输入，输出逐字不一致。

    **严重度按「这次任务有没有要求改行为」分档**（这是判据的一部分，不是
    措辞偏好）：

    - **优化/提速类任务** → 高严重度。任务原话就是「结果别改坏」，行为变了
      就是没做到——探针 `vague-optimize` 的 Counter 案例正是这一档。
    - **其余任务** → 提示。任务本来就要改行为（合并页面、加功能、跨文件改名
      都可能改动输出），「输出变了」在那些任务上是**预期**，把它报成高严重度
      会去拦一次正确的完成声明（§4：L1/L2 有误报就是判据写错了）。
      这一档仍然逐条写进收尾报告，只是不拦完成声明——真正的缺陷（引用断裂、
      测试坏了、接口被删）各自有 L1 与 T1 那条通道在拦。
    """
    severity = HIGH if report.optimization else INFO
    if report.optimization:
        summary = (
            "行为回归（L2.2 对照运行）：这条命令的输出与改动前**逐字不一致**，"
            "而这次任务要的是「更快、结果不变」——**变快了也不算通过**"
        )
    else:
        summary = (
            "行为变化（L2.2 对照运行）：这条命令的输出与改动前**逐字不一致**，"
            "任务里没有要求改变它的行为（记下来给人看，不当成阻断理由）"
        )
    detail = (
        f"{report.command}（输入 {report.input_path}）；退出码 "
        f"{first.code} → {second.code}；"
        f"改动前 stdout: {_excerpt(first.stdout) or '（空）'}；"
        f"改动后 stdout: {_excerpt(second.stdout) or '（空）'}"
    )
    return Finding(
        kind=BEHAVIOR_CHANGED_KIND,
        severity=severity,
        summary=summary,
        detail=detail,
        path=report.input_path,
        # `escalate` 只标「该动硬手段」的那一档；非优化类任务上的行为变化
        # 只是一条给人看的证据（与 `audit.tampering_suspected` 同一个口径）。
        escalate=severity == HIGH,
    )


def _performance_findings(report: BehaviorReport) -> list[Finding]:
    """L2.4：优化类任务要有实测改善**且**行为未变。"""
    if not report.optimization:
        return []
    if report.optimization and report.changed:
        # 这一条是 Counter 那个案例的正解：变快了而输出变了 → 不算通过。
        #
        # （`report.optimization` 那一半在函数入口已经判过；写在这里是让
        # 「为什么它必须与分类同时成立」在代码上也读得出来。）
        report.faster = False
        return [
            Finding(
                kind=PERFORMANCE_REGRESSED_KIND,
                severity=HIGH,
                summary=(
                    "优化任务不通过：行为已经变了，**再快也不算优化通过**"
                    "（要的是「结果一点都不能变」）"
                ),
                detail=(
                    f"{report.command}：改动前 {report.before_seconds:.3f}s / "
                    f"改动后 {report.after_seconds:.3f}s，而输出逐字不一致"
                ),
            )
        ]
    base, now = report.before_seconds, report.after_seconds
    if base < PERF_FLOOR_SECONDS:
        # 跑得太快，计时噪声比差值大：**不判快慢**，如实说这一条没测出来。
        report.faster = None
        return [
            Finding(
                kind=BEHAVIOR_NOT_CHECKED_KIND,
                severity=INFO,
                summary=(
                    f"{CANNOT_CHECK}性能不倒退（L2.4）：改动前只跑了 "
                    f"{base:.3f}s（低于标定下限 {PERF_FLOOR_SECONDS:.2f}s），"
                    "这个量级上计时噪声比差值大，判不出快慢"
                ),
                detail="行为比对仍然有效（输出逐字相同），只是耗时这一条没测出来",
            )
        ]
    if now <= base * PERF_IMPROVE_RATIO:
        report.faster = True
        return []
    if now <= base * PERF_REGRESS_RATIO:
        # 落在两组线之间（没快到 15%，也没慢过 25%）：**不判**，只记读数。
        # 这一段正是计时噪声带，把它判成「倒退」就是拿噪声去拦正确的完成。
        report.faster = None
        return [
            Finding(
                kind=BEHAVIOR_NOT_CHECKED_KIND,
                severity=INFO,
                summary=(
                    f"{CANNOT_CHECK}性能不倒退（L2.4）：改动前 {base:.3f}s / "
                    f"改动后 {now:.3f}s，落在噪声带里"
                    f"（改善线 {int((1 - PERF_IMPROVE_RATIO) * 100)}% 以上、"
                    f"倒退线 {int((PERF_REGRESS_RATIO - 1) * 100)}% 以上才算数）"
                ),
                detail="行为比对仍然有效（输出逐字相同），只是这一次量不出快慢",
            )
        ]
    report.faster = False
    return [
        Finding(
            kind=PERFORMANCE_REGRESSED_KIND,
            severity=HIGH,
            summary=(
                "优化任务不通过：**性能倒退**"
                f"（改动前 {base:.3f}s → 改动后 {now:.3f}s，"
                f"比改动前慢 {int((now / base - 1) * 100)}%）"
            ),
            detail=f"{report.command}（输入 {report.input_path}）同一份输入实测",
        )
    ]


# ---- L2.3 的另一半：CLI `--help` 文本快照 ------------------------------------


def cli_help_candidates(root: Path, changed: Sequence[str]) -> list[str]:
    """改动触及的、**看起来像命令行入口**的脚本（`--help` 快照只对这些跑）。

    判据是机械的：改动过的顶层 `.py`，且正文里有 `argparse` / `ArgumentParser`
    / `if __name__` 三者之一。没有就返回空——一个没有 CLI 的项目没有
    「CLI 接口」可比（那不是漏判，是这一条对它不适用）。
    """
    out: list[str] = []
    for stored in changed:
        pure = Path(stored)
        if pure.is_absolute() or pure.suffix != ".py" or pure.parent != Path("."):
            continue
        text = _read_text(root / stored)
        if not text:
            continue
        if not any(
            token in text
            for token in ("argparse", "ArgumentParser", "__main__", "sys.argv")
        ):
            continue
        out.append(Path(stored).as_posix())
    return sorted(set(out))


def compare_cli_help(
    root: Path,
    changes: Sequence[PendingChange],
    files: Sequence[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    runner: Runner | None = None,
) -> tuple[bool | None, list[Finding]]:
    """对入口脚本跑 `--help`，改动前后逐字比对。

    这一条补的是 L2.3 里 `ast` 快照看不到的那一半：子命令被删掉、参数被改名，
    公开符号一个都没变，而用户看得见的接口变了。判据只认「两边都跑出东西
    且文本不同」——一边跑不起来时**不报**（那条不是接口变化，是入口本身跑不动，
    由 L2.2 的读数说话）。
    """
    if not files:
        return None, []
    items = [item for item in changes if not Path(item.path).is_absolute()]
    if not items:
        return None, []
    entry = files[0]
    run = runner or _spawn
    command = ("python", entry, "--help")
    try:
        with _Trial(root, _baseline_changes(items), "surface-before") as before_root:
            with _Trial(root, list(items), "surface-after") as after_root:
                first = run(before_root, command, timeout)
                second = run(after_root, command, timeout)
                if not (first.ran and second.ran):
                    return None, []
                same = _digest(first, (before_root, after_root)) == _digest(
                    second, (before_root, after_root)
                )
                if same:
                    return True, []
                return False, [
                    Finding(
                        kind=CLI_SURFACE_CHANGED_KIND,
                        severity=HIGH,
                        summary=(
                            f"命令行接口变了：`{entry} --help` 的输出与改动前"
                            "不一致（删除/改名子命令或参数）"
                        ),
                        detail=(
                            f"改动前: {_excerpt(first.stdout or first.stderr)}；"
                            f"改动后: {_excerpt(second.stdout or second.stderr)}"
                        ),
                        path=entry,
                    )
                ]
    except OSError:
        return None, []
