"""热记忆文件。

热记忆是每轮都要注入上下文的那一层，所以它必须被硬性限制大小。
这个限制不是约束，而是新陈代谢机制：写满就按优先级把最不耐久的内容
下沉到冷记忆，否则它会慢慢吃掉全部上下文预算。

文件是人类可读的 Markdown，用户可以直接手工编辑——这是有意为之：
记忆内容若无法被人工审视和修正，错误的记忆会长期污染行为。
"""

from pathlib import Path

from agents_dev import limits as _limits
from agents_dev.llm.tokenizer import TokenCounter

HOT_TITLE = "# 项目记忆（自动维护，也可手动编辑）"

SECTION_TITLES: dict[str, str] = {
    "fact": "项目事实",
    "preference": "用户偏好",
    "decision": "关键决策",
    "lesson": "教训",
    "issue": "已知问题",
    "doing": "进行中",
}

_TITLE_TO_KIND = {title: kind for kind, title in SECTION_TITLES.items()}

# 超容量时的下沉顺序：越靠前越先被移出热记忆。
# 事实最稳定、复用价值最高，所以最后才动它。
DEMOTE_ORDER: tuple[str, ...] = (
    "doing",
    "issue",
    "decision",
    "lesson",
    "preference",
    "fact",
)

# 「热记忆占多少」这个数只有一个出处：登记表。装配层那个配额
# （budget.quota("hot_memory")）也从同一个名字取值——两处各写一份时，
# 改一处不动另一处，比没有这个旋钮更糟。
HOT_RATIO = float(_limits.knob("hot_memory_ratio").default)


def hot_budget(
    context_window: int,
    ratio: float | None = None,
    overrides: dict | None = None,
) -> int:
    """热记忆的 token 上限，按有效预算（扣除输出预留）的比例计算。"""
    from agents_dev.context.budget import OUTPUT_RESERVE_RATIO

    if ratio is None:
        ratio = float(_limits.resolve("hot_memory_ratio", overrides)[0])

    effective = int(context_window * (1 - OUTPUT_RESERVE_RATIO))
    return int(effective * ratio)


def render_hot(sections: dict[str, list[str]]) -> str:
    """渲染成 Markdown。空分节不输出。"""
    lines = [HOT_TITLE, ""]
    for kind in SECTION_TITLES:
        items = sections.get(kind) or []
        if not items:
            continue
        lines.append(f"## {SECTION_TITLES[kind]}")
        lines.extend(f"- {item}" for item in items)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def parse_hot(text: str) -> dict[str, list[str]]:
    """解析 Markdown。未知分节被忽略——它们不参与注入，留着也只会浪费预算。"""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            current = _TITLE_TO_KIND.get(line[3:].strip())
            if current is not None:
                sections.setdefault(current, [])
            continue
        if current and line.startswith("- "):
            sections[current].append(line[2:].strip())
    return {kind: items for kind, items in sections.items() if items}


def trim_to_budget(
    sections: dict[str, list[str]],
    counter: TokenCounter,
    budget: int,
) -> tuple[dict[str, list[str]], list[str]]:
    """把热记忆裁剪到预算内，返回（保留的分节，被下沉的条目）。"""
    working = {kind: list(items) for kind, items in sections.items()}
    demoted: list[str] = []

    for kind in DEMOTE_ORDER:
        while working.get(kind) and counter.count(render_hot(working)) > budget:
            demoted.append(working[kind].pop(0))
        if not working.get(kind):
            working.pop(kind, None)

    return {kind: items for kind, items in working.items() if items}, demoted


def write_hot(path: Path, sections: dict[str, list[str]]) -> None:
    """写入热记忆文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_hot(sections), encoding="utf-8")


def read_hot(path: Path) -> dict[str, list[str]]:
    """读取热记忆文件，不存在则返回空。"""
    if not path.exists():
        return {}
    return parse_hot(path.read_text(encoding="utf-8"))

