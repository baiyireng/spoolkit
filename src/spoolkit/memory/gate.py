"""写入门禁：什么内容才配进热记忆。

设计稿 §5.1 把「什么值得记」写成五条判据（J1..J5）。这些判据必须是**代码**，
而不是提示词里的一句约束：实测证据是同一个模型产出的三条逐字相同的寒暄
（真实工作区 `memory.md:4/5/8`，设计稿 §2.5）——提示词约束对它无效，
两条重复的条目照样能落进每轮注入的热记忆。

P0 只实现 J1/J3/J4，以及 J5 的「lesson 必须有触发词」；J2（`evidence` 非空）
与 `scope` 要给蒸馏 schema 加必填字段，属 P1（设计稿 §5.2），这里**不假装判过**。

判据的方向是**降级而不是丢弃**：不过的条目由 `archive.py` 的 promote 循环写进
`memory.kind='episode'`，理由一并记在 `ArchiveResult.downgraded` 里——
「发生过」这件事本身有价值，只是不该每轮注入（设计稿 §5.3 ②）。

阈值一律从 `limits` 取，不在本模块里写死（本仓既有规矩，设计稿 §4.3）。
"""

import re
from dataclasses import dataclass
from typing import Callable

from spoolkit import limits
from spoolkit.memory.transcript import redact_secrets_report

# 一次性指代与「请确认」：这类话只对这一次任务成立，换个目标就没意义（J1）。
ONESHOT_MARKERS: tuple[str, ...] = (
    "这次",
    "本次",
    "刚才",
    "上面的",
    "请确认",
    "已完成",
    "已经完成",
)

# 寒暄、能力自述、致谢、复述用户的话（J3）。
GREETING_MARKERS: tuple[str, ...] = (
    "你好",
    "您好",
    "我是你的",
    "我是本地",
    "有什么可以帮",
    "感谢",
    "谢谢",
    "你之前问过",
)

# 交付答复模板的开头：出现的几乎都是「某次交付的正文」，不是可复用事实（J4）。
DELIVERY_PREFIXES: tuple[str, ...] = (
    "已完成",
    "已经实现完毕",
    "文件为",
    "运行方式",
    "操作说明",
    "功能：",
    "功能:",
)

_CODE_FENCE = re.compile(r"^\s*(```|~~~)", re.MULTILINE)


@dataclass(frozen=True)
class Verdict:
    """一条判据的结论。不过时 `reason` 里带判据编号与一句可打印的理由。"""

    ok: bool
    reason: str = ""


@dataclass(frozen=True)
class _Target:
    """一次判定要看的东西。"""

    kind: str
    text: str
    trigger: str
    max_chars: int
    table_lines: int


@dataclass(frozen=True)
class Rule:
    """一条判据：`check` 命中返回细节，通过返回空串。

    `id` 与 `title` 是给人看的一半——拒绝的理由要能直接读出来，
    否则调这块还是猜（设计稿 §5.1 末段）。
    """

    id: str
    title: str
    check: Callable[[_Target], str]


def _first_marker(text: str, markers: tuple[str, ...]) -> str:
    for marker in markers:
        if marker in text:
            return marker
    return ""


def _table_rows(text: str) -> int:
    """数出「同构表格」的行数：一行里至少两个 `|` 才算一格表行。"""
    return sum(1 for line in text.splitlines() if line.count("|") >= 2)


def _j1(target: _Target) -> str:
    hit = _first_marker(target.text, ONESHOT_MARKERS)
    if not hit:
        return ""
    return f"命中一次性指代「{hit}」，换一个任务目标就不再成立"


def _j3(target: _Target) -> str:
    hit = _first_marker(target.text, GREETING_MARKERS)
    if not hit:
        return ""
    return f"命中寒暄/自述/致谢「{hit}」，不是项目事实"


def _j4(target: _Target) -> str:
    length = len(target.text)
    if length > target.max_chars:
        return (
            f"长度 {length} 超过 remember_max_chars={target.max_chars}："
            "整段答复/长解释不是一句话事实"
        )
    rows = _table_rows(target.text)
    if rows >= target.table_lines:
        return f"含 {rows} 行同构表格（≥{target.table_lines} 行即一次性产物）"
    if _CODE_FENCE.search(target.text):
        return "含代码围栏：交付正文不是一句话事实"
    for prefix in DELIVERY_PREFIXES:
        if target.text.startswith(prefix):
            return f"以交付模板开头「{prefix}」：一次性交付答复"
    return ""


def _j5(target: _Target) -> str:
    if target.kind != "lesson":
        return ""
    if target.trigger:
        return ""
    return "lesson 没有触发词：它永远不会被推到该出现的场景"


# 判据表。顺序即判定顺序：被多条命中时以靠前的理由为准。
RULES: tuple[Rule, ...] = (
    Rule("J1", "跨任务可复用", _j1),
    Rule("J3", "非寒暄/非自述", _j3),
    Rule("J4", "非一次性产物", _j4),
    Rule("J5", "有明确触发", _j5),
)

RULES_BY_ID: dict[str, Rule] = {rule.id: rule for rule in RULES}


def judge(
    kind: str,
    text: str,
    trigger: str = "",
    *,
    overrides: dict | None = None,
) -> Verdict:
    """判定一条候选条目能不能进热记忆。

    `overrides` 与别处同一口径（`.agent/limits.json` 的覆盖值）：
    判据的阈值也必须能被按工作区覆盖，否则「调一版判据」就得改代码。
    """
    target = _Target(
        kind=kind,
        text=text.strip(),
        trigger=trigger.strip(),
        max_chars=int(limits.resolve("remember_max_chars", overrides)[0]),
        table_lines=int(limits.resolve("remember_table_lines", overrides)[0]),
    )
    if not target.text:
        return Verdict(False, "空条目：没有任何内容可记")
    for rule in RULES:
        detail = rule.check(target)
        if detail:
            return Verdict(False, f"{rule.id} {rule.title}（{detail}）")
    return Verdict(True)


def sanitize(text: str) -> tuple[str, list[str]]:
    """写入前的脱敏（设计稿 §5.3 ①）。

    与读路径共用 `transcript.redact_secrets` 的同一份形状表：写入侧再抄一遍正则，
    迟早两份会不一致。返回（脱敏后的文本，命中的形状名）——形状名可以写进理由，
    **值本身不回显**（理由里带上密钥值等于把它又抄了一遍）。
    """
    return redact_secrets_report(text)
