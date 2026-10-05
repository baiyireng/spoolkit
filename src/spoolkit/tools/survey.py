"""一次把「这一处该看的东西」收齐。

这不是「每轮必经的信息收集阶段」——那笔账算过：小题里 52 次写入中有 48 次
发生在「整个任务零次 read_file」的情况下，它们本来就不需要先看。
**它是一个动作**：模型自己决定什么时候要看、看哪一处，一次拿全。

它省的是**步数**，不是 token：原先那条路是「列目录 → 猜文件名 → 读 →
猜错了换一个再读」。实测模型猜错过（`read_file 03_add_function/word.py`
不存在），那一步就白花了。现在一次调用拿到「目录里有什么 + 契约 + 代码」。

**为什么测试文件优先给全文**：它就是契约——固定了模块对外长什么样
（导入哪个名字、怎么调用、返回什么）。实测那三处「顺手把代码改得更合理」
的失败（重命名函数、换签名、改返回值形状）全都是没看它。

预算是有界的：给不完就明说「哪些没给」，而不是悄悄少给。
"""

from pathlib import Path, PurePosixPath

from spoolkit.errors import PathOutsideProjectError
from spoolkit.paths import resolve_readable
from spoolkit.tools.types import ToolResult, ToolSpec
from spoolkit.tools.view import WorkspaceView

DEFAULT_BUDGET = 1200
# 只收这些后缀：二进制塞进上下文既是噪声也是浪费。
_TEXT_SUFFIXES = (
    ".py", ".pyi", ".md", ".txt", ".json", ".toml", ".cfg", ".ini",
    ".yaml", ".yml", ".js", ".ts", ".tsx", ".html", ".css", ".sh", ".sql",
)


def _is_test(name: str) -> bool:
    return name.startswith("test_") or name.endswith(("_test.py", "_test.ts"))


def _entries(base: Path, view: WorkspaceView, skipped: list[str]) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    try:
        children = sorted(base.iterdir())
    except OSError:
        # 目录本身读不了：记下来，由调用方给人话（不许抛出去，也不许当空目录）。
        skipped.append(str(base))
        children = []
    for item in children:
        try:
            if item.is_dir():
                found.append((item.name + "/", -1))
            elif item.is_file():
                found.append((item.name, item.stat().st_size))
        except OSError:
            # 读不了的条目要留痕：静默跳过等于告诉模型「这里没有它」。
            skipped.append(str(item))
            continue
    known = {name.rstrip("/") for name, _ in found}
    for name in view.overlay_children(base):
        if name not in known:
            found.append((name, 0))
    return found


def _files_to_read(base: Path, skipped: list[str]) -> list[Path]:
    """先契约（测试），再小的先给——预算不够时先保最要紧的。"""
    candidates: list[Path] = []
    try:
        children = sorted(base.iterdir())
    except OSError:
        skipped.append(str(base))
        return candidates
    for item in children:
        try:
            if not item.is_file():
                continue
        except OSError:
            skipped.append(str(item))
            continue
        if item.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        candidates.append(item)
    candidates.sort(key=lambda p: (not _is_test(p.name), p.stat().st_size))
    return candidates


def _skipped_note(skipped: list[str], limit: int = 5) -> str:
    """跳过的东西必须可见（静默跳过＝假装这一处已经收齐了）。

    同一个路径可能被两条路各记一次（列举失败 + 读失败），这里按首次出现去重：
    把一处报成两处，读数就不是「如实」了。
    """
    unique: list[str] = []
    for item in skipped:
        if item not in unique:
            unique.append(item)
    shown = unique[:limit]
    more = len(unique) - len(shown)
    return (
        f"（跳过 {len(unique)} 个读不了的路径：{'、'.join(shown)}"
        + (f"，还有 {more} 个" if more else "")
        + "）"
    )


def survey_spec(
    root: Path,
    tokenizer,
    pending=None,
    read_roots=(),
    default_budget: int = DEFAULT_BUDGET,
) -> ToolSpec:
    view = WorkspaceView(root, pending)

    def handler(args: dict) -> ToolResult:
        raw = str(args.get("path") or ".")
        try:
            base = resolve_readable(root, raw, read_roots)
        except PathOutsideProjectError as exc:
            return ToolResult(ok=False, content=str(exc))
        if not base.is_dir():
            # 失败话术要**回应它想干什么**，而不是只给一条替代方案。
            #
            # 原先只有一句「要看单个文件用 read_file」。dogfood 那条轨迹里
            # 模型仍然原样重试了一次——因为「看一个文件」有两种完全不同的
            # 目的：要**内容**（read_file）、要**它属于哪儿/还有什么**
            # （list_dir 或对父目录 survey）。只说前者，后者那类意图就被
            # 判成「它不听话」，而不是「我们没有它要的那个动作」。
            # 父目录用 posix 形态：工作区里的路径一律按 `/` 写（提示词与
            # 报错话术用的都是这个约定），Windows 上给它一个反斜杠路径，
            # 模型下一轮很可能照着抄回去，而它自己不知道那是同一个地方。
            parent = str(PurePosixPath(raw.replace("\\", "/")).parent) or "."
            return ToolResult(
                ok=False,
                content=(
                    f"不是目录: {raw}（survey 只对目录生效）。"
                    "按你的意图选一个：要看这个文件的**内容** → read_file(\""
                    f"{raw}\", start_line=…, end_line=…) 只取需要的行区间；"
                    "要看**它在哪儿、这个目录里还有什么** → list_dir(\""
                    f"{parent}\")；"
                    "要把**这一处的代码与验收测试一次收齐** → 对它所处的目录调 survey。"
                ),
            )

        budget = int(args.get("budget") or default_budget)
        budget = max(200, min(budget, 4000))

        skipped: list[str] = []
        entries = _entries(base, view, skipped)
        lines = [f"== {raw} 里有什么 =="]
        lines += [
            f"{name + '/' if size < 0 else f'{name} ({size}B)'}" for name, size in entries[:40]
        ]
        if len(entries) > 40:
            lines.append(f"…（还有 {len(entries) - 40} 项）")

        used = tokenizer.count("\n".join(lines))
        skipped_budget: list[str] = []
        for item in _files_to_read(base, skipped):
            try:
                text = view.read_text(item)
            except (OSError, UnicodeDecodeError):
                # 读不了的文件要留痕（它是「这一处该看的东西」，没给就得说清）。
                skipped.append(str(item))
                continue
            head = "测试（契约）" if _is_test(item.name) else "代码"
            block = f"\n== {head}: {item.name} ==\n{text.rstrip()}"
            cost = tokenizer.count(block)
            if used + cost > budget:
                skipped_budget.append(item.name)
                continue
            lines.append(block)
            used += cost

        if skipped_budget:
            lines.append(
                f"\n（预算 {budget} 用完，这些没给：{'、'.join(skipped_budget)}"
                "——要用就用 read_file 单独取）"
            )
        if skipped:
            lines.append(_skipped_note(skipped))
        return ToolResult(ok=True, content="\n".join(lines))

    return ToolSpec(
        name="survey",
        description=(
            "一次收齐「这一处该看的东西」：这个目录里有什么、它的验收测试"
            "（契约：固定了模块对外长什么样）、以及目录里的代码。"
            "**动手改一处之前用它**，比「列目录 → 猜文件名 → 读 → 猜错了再换」"
            "省好几步。path 必须是**目录**：要看单个文件的内容用 read_file，"
            "要只看目录里有什么用 list_dir。默认只收文本文件，总量有预算，"
            "给不完会明说。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要看的目录（必须是目录，不是单个文件），默认当前目录",
                },
                "budget": {
                    "type": "integer",
                    "description": "最多收多少 token，默认 1200（目录很大时调大）",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=handler,
        brief="一次收齐这一处该看的",
        group="看",
    )
