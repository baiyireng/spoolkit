"""热记忆文件。

热记忆是每轮都要注入上下文的那一层，所以它必须被硬性限制大小。
这个限制不是约束，而是自动淘汰机制：写满就按优先级把最不耐久的内容
下沉到冷记忆，否则它会逐步占满全部上下文预算。
**「按优先级」在分节内部也要成立**：同为 `fact`，一条被 5 个任务引用过的
构建命令必须比一条没被引用过的目录约定活得久（设计稿 §7.1）。

文件是人类可读的 Markdown，用户可以直接手工编辑——这是有意为之：
记忆内容若无法被人工审视和修正，错误的记忆会长期污染行为。
"""

import re
from dataclasses import dataclass
from typing import Callable
from pathlib import Path

from spoolkit import limits as _limits
from spoolkit.llm.tokenizer import TokenCounter

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
HOT_RATIO = float(_limits.require_knob("hot_memory_ratio").default)

# ---- 分节内的重要度启发式（设计稿 §7.1 的 P0 近似，见 importance_hint）----

# 一次性指代：与写入门禁（gate.ONESHOT_MARKERS）同一套词的用意——那句话
# 换个任务目标就不再成立，所以它先走。
ONESHOT_MARKERS: tuple[str, ...] = (
    "这次",
    "本次",
    "刚才",
    "上面的",
    "请确认",
    "已完成",
    "已经完成",
)

# 复述用户的话：它记的是"用户问过什么"，不是项目事实。
USER_RETELL_MARKERS: tuple[str, ...] = ("你之前问过", "你说过", "你提到过")

# 证据形状。三类都要求文本里有一个**能回去核对**的东西：
# 反引号里的路径/命令、`文件:行`、以及以命令名开头的一行。
_FILE_LINE = re.compile(r"[\w./\\-]+\.[A-Za-z0-9]+:\d+")
_COMMAND_START = re.compile(
    r"^\s*(pytest|python|pip|uv|npm|pnpm|yarn|node|git|cargo|go|make|docker)\b"
)


@dataclass(frozen=True)
class ImportanceRule:
    """一条重要度规则：`points` 给出它加/扣多少分。

    分值不写死在调用处：规则表是唯一定义处，调一档只改这一张表，
    报告里的分值差也就能追到具体某条规则。
    """

    id: str
    reason: str
    points: Callable[[str], int]


def _points_evidence(text: str) -> int:
    return 2 * len(_evidence_shapes(text))


def _points_oneshot(text: str) -> int:
    return -2 if _first_marker(text, ONESHOT_MARKERS) else 0


def _points_retell(text: str) -> int:
    return -1 if _first_marker(text, USER_RETELL_MARKERS) else 0


# 顺序即说明顺序。分值都写在 `points` 里，不散落在调用处。
_IMPORTANCE_RULES: tuple[ImportanceRule, ...] = (
    ImportanceRule("I1", "含可核对的证据形状（路径/命令/文件:行）", _points_evidence),
    ImportanceRule("I2", "含一次性指代（换个任务就作废）", _points_oneshot),
    ImportanceRule("I3", "复述用户的话（不是项目事实）", _points_retell),
)


def hot_budget(
    context_window: int,
    ratio: float | None = None,
    overrides: dict | None = None,
) -> int:
    """热记忆的 token 上限，按有效预算（扣除输出预留）的比例计算。"""
    from spoolkit.context.budget import OUTPUT_RESERVE_RATIO

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


def importance_hint(text: str, overrides: dict | None = None) -> int:
    """热记忆条目在**同一分节内部**的淘汰优先级（越大越该留下）。

    这是设计稿 §7.1 三元组 `(重要性, confidence, created_at)` 的 **P0 近似**：
    热记忆是纯 Markdown、没有逐条元数据，所以拿不出来的维度（confidence /
    created_at）不假装有，只用**文本本身可解释的形状**打分：

    | 规则 | 分值 | 依据 |
    |---|---|---|
    | 含反引号引用的路径或命令 | +2 | 能回去核对 |
    | 含 `文件:行` 定位 | +2 | 能回去核对 |
    | 以命令名开头（`pytest -q`、`npm run build`） | +1 | 能回去核对 |
    | 命中一次性指代（这次/本次/刚才/…） | -2 | 换个任务就作废 |
    | 命中复述用户的话（你之前问过/你说过/…） | -1 | 不是项目事实 |
    | 每满 `hot_importance_long_step` 字 | -1 | 越长越像某次交付的正文 |

    依据是同一个：**可核对的具体证据留得住价值，泛泛的即时描述留不住**。
    规则表 `_IMPORTANCE_RULES` 是分值的唯一定义处，报告与注释不会各写一套。
    """
    step = max(1, int(_limits.resolve("hot_importance_long_step", overrides)[0]))
    return sum(rule.points(text) for rule in _IMPORTANCE_RULES) - len(text) // step


def _evidence_shapes(text: str) -> tuple[str, ...]:
    """数出「证据形状」：能指到具体文件/命令/位置的条目才值得长期留着。"""
    shapes: list[str] = []
    if "`" in text:
        shapes.append("含反引号引用的路径或命令")
    if _FILE_LINE.search(text):
        shapes.append("含 文件:行 定位")
    if _COMMAND_START.match(text):
        shapes.append("以命令名开头")
    return tuple(shapes)


def _first_marker(text: str, markers: tuple[str, ...]) -> str:
    for marker in markers:
        if marker in text:
            return marker
    return ""


def trim_to_budget(
    sections: dict[str, list[str]],
    counter: TokenCounter,
    budget: int,
    *,
    max_items: int | None = None,
    overrides: dict | None = None,
) -> tuple[dict[str, list[str]], list[str]]:
    """把热记忆裁剪到容量内，返回（保留的分节，被下沉的条目）。

    两条容量线（设计稿 §4.3 的容量口径）：

    - **token 预算** `budget`（`hot_budget`，来自 `hot_memory_ratio`）；
    - **条数上限** `hot_max_items`（默认 40）。token 只约束总字数，约束不了
      「条数」——40 条各一行读得动，400 条一行也读不动。

    超限时**按分节下沉顺序 + 分节内重要度**选牺牲者：

    1. 先按 `DEMOTE_ORDER` 走分节（`doing` → … → `fact`，事实最后动）；
    2. 同一个分节内部不再从最前面拿（那是先进先出，与"事实最后才动"这条
       注释相反，见 `test_同分节内低重要度条目先被淘汰`），而是淘汰
       `importance_hint(text)` **最低**的那条；同分则淘汰**位置更靠前**的
       （位置只作 tie-break，保留既有顺序语义）。

    重要度是**启发式近似**，不是设计稿 §7.1 的完整三元组
    `(重要性, confidence, created_at)`：P0 的热记忆是纯 Markdown、没有逐条
    元数据（决议 6 要求格式不变），`confidence`/`created_at` 没有承载。
    P1 拿到 `semantic` 表的元数据后按三元组替换这里的位置 tie-break。
    """
    working = {kind: list(items) for kind, items in sections.items()}
    demoted: list[str] = []
    if max_items is None:
        max_items = int(_limits.resolve("hot_max_items", overrides)[0])
    limit = max(0, int(max_items))

    for kind in DEMOTE_ORDER:
        while working.get(kind) and _over_capacity(working, counter, budget, limit):
            # 分节内淘汰：重要度最低的先走，位置靠前的作 tie-break。
            victim = min(
                range(len(working[kind])),
                key=lambda index: (importance_hint(working[kind][index], overrides), index),
            )
            demoted.append(working[kind].pop(victim))
        if not working.get(kind):
            working.pop(kind, None)

    return {kind: items for kind, items in working.items() if items}, demoted


def _over_capacity(
    working: dict[str, list[str]],
    counter: TokenCounter,
    budget: int,
    limit: int,
) -> bool:
    """两条容量线任一条超了就算超。"""
    return counter.count(render_hot(working)) > budget or _count_items(working) > limit


def _count_items(working: dict[str, list[str]]) -> int:
    return sum(len(items) for items in working.values())


def write_hot(path: Path, sections: dict[str, list[str]]) -> None:
    """写入热记忆文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_hot(sections), encoding="utf-8")


def read_hot(path: Path) -> dict[str, list[str]]:
    """读取热记忆文件，不存在则返回空。"""
    if not path.exists():
        return {}
    return parse_hot(path.read_text(encoding="utf-8"))

