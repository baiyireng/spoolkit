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

from pathlib import Path

from agents_dev.errors import PathOutsideProjectError
from agents_dev.paths import resolve_readable
from agents_dev.tools.types import ToolResult, ToolSpec
from agents_dev.tools.view import WorkspaceView

DEFAULT_BUDGET = 1200
# 只收这些后缀：二进制塞进上下文既是噪声也是浪费。
_TEXT_SUFFIXES = (
    ".py", ".pyi", ".md", ".txt", ".json", ".toml", ".cfg", ".ini",
    ".yaml", ".yml", ".js", ".ts", ".tsx", ".html", ".css", ".sh", ".sql",
)


def _is_test(name: str) -> bool:
    return name.startswith("test_") or name.endswith(("_test.py", "_test.ts"))


def _entries(base: Path, view: WorkspaceView) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for item in sorted(base.iterdir()):
        try:
            if item.is_dir():
                found.append((item.name + "/", -1))
            elif item.is_file():
                found.append((item.name, item.stat().st_size))
        except OSError:
            continue
    known = {name.rstrip("/") for name, _ in found}
    for name in view.overlay_children(base):
        if name not in known:
            found.append((name, 0))
    return found


def _files_to_read(base: Path) -> list[Path]:
    """先契约（测试），再小的先给——预算不够时先保最要紧的。"""
    candidates: list[Path] = []
    for item in sorted(base.iterdir()):
        try:
            if not item.is_file():
                continue
        except OSError:
            continue
        if item.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        candidates.append(item)
    candidates.sort(key=lambda p: (not _is_test(p.name), p.stat().st_size))
    return candidates


def survey_spec(root: Path, tokenizer, pending=None, read_roots=()) -> ToolSpec:
    view = WorkspaceView(root, pending)

    def handler(args: dict) -> ToolResult:
        raw = str(args.get("path") or ".")
        try:
            base = resolve_readable(root, raw, read_roots)
        except PathOutsideProjectError as exc:
            return ToolResult(ok=False, content=str(exc))
        if not base.is_dir():
            return ToolResult(
                ok=False, content=f"不是目录: {raw}（要看单个文件用 read_file）"
            )

        budget = int(args.get("budget") or DEFAULT_BUDGET)
        budget = max(200, min(budget, 4000))

        entries = _entries(base, view)
        lines = [f"== {raw} 里有什么 =="]
        lines += [
            f"{name + '/' if size < 0 else f'{name} ({size}B)'}" for name, size in entries[:40]
        ]
        if len(entries) > 40:
            lines.append(f"…（还有 {len(entries) - 40} 项）")

        used = tokenizer.count("\n".join(lines))
        skipped: list[str] = []
        for item in _files_to_read(base):
            try:
                text = view.read_text(item)
            except (OSError, UnicodeDecodeError):
                continue
            head = "测试（契约）" if _is_test(item.name) else "代码"
            block = f"\n== {head}: {item.name} ==\n{text.rstrip()}"
            cost = tokenizer.count(block)
            if used + cost > budget:
                skipped.append(item.name)
                continue
            lines.append(block)
            used += cost

        if skipped:
            lines.append(
                f"\n（预算 {budget} 用完，这些没给：{'、'.join(skipped)}"
                "——要用就用 read_file 单独取）"
            )
        return ToolResult(ok=True, content="\n".join(lines))

    return ToolSpec(
        name="survey",
        description=(
            "一次收齐「这一处该看的东西」：这个目录里有什么、它的验收测试"
            "（契约：固定了模块对外长什么样）、以及目录里的代码。"
            "**动手改一处之前用它**，比「列目录 → 猜文件名 → 读 → 猜错了再换」"
            "省好几步。默认只收文本文件，总量有预算，给不完会明说。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要看的目录，默认当前目录"},
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
