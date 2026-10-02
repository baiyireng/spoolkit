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

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Callable

from spoolkit import limits
from spoolkit.memory.transcript import redact_secrets_report

# 一次性指代与「请确认」：这类话只对这一次任务成立，换个目标就没意义（J1）。
# 「你之前问过」也在这里：它记的是**上一次那个任务**问过什么，换一个目标就不成立。
# 真实工作区 `memory.md:6` 是这一条（计划 Task 7 的判据点名它落 J1）。
ONESHOT_MARKERS: tuple[str, ...] = (
    "这次",
    "本次",
    "刚才",
    "上面的",
    "请确认",
    "你之前问过",
    "已完成",
    "已经完成",
)

# 寒暄、能力自述、致谢（J3）。**复述用户的话不在这里**：「你之前问过两次…」
# 记的是"上一次那个任务问过什么"，换一个目标就不成立，所以它落 J1
# （`ONESHOT_MARKERS` 里那条「你之前问过」）——两条判据的方向不同，别混。
# 真实工作区 `memory.md:6` 就是这一条。
GREETING_MARKERS: tuple[str, ...] = (
    "你好",
    "您好",
    "我是你的",
    "我是本地",
    "有什么可以帮",
    "感谢",
    "谢谢",
)

# 交付答复模板的**开头**：出现的几乎都是「某次交付的正文」，不是可复用事实（J4）。
DELIVERY_PREFIXES: tuple[str, ...] = (
    "已完成",
    "已经实现完毕",
    "文件为",
    "运行方式",
    "操作说明",
    "功能：",
    "功能:",
)

# 交付答复里的**加粗小标题**（`**运行方式：**`、`**操作说明：**`）：它们出现在
# 正文中间，不在开头（真实工作区 `memory.md:9/11` 就是这样），所以不能只靠
# `startswith`。判据本身没变——「以交付模板开头」扩成「带交付模板的加粗小标题」，
# 依据是同一个：这几句是**某次交付的正文**在自我介绍，不是可复用事实。
#
# 只收「加粗 + 小标题」这几种形状：`运行方式` / `操作说明` 单独出现时可能是
# 一条真事实（"运行方式是 uv run"），加粗成小标题才是交付正文。
DELIVERY_MARKERS: tuple[str, ...] = (
    "运行方式",
    "操作说明",
    "使用说明",
    "功能：",
    "功能:",
    "文件为",
)

_CODE_FENCE = re.compile(r"(```|~~~)")
# `**运行方式` / `**运行方式：**` 都要命中：小标题后面跟不跟冒号是排版差异。
_DELIVERY_MARKER = re.compile(
    r"\*\*\s*(" + "|".join(DELIVERY_MARKERS) + r")"
)


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
    """数出「同构表格」有几行。

    「一个格」= 被 `|` 分开的一段非空文本；「一行表」= 一行里**至少 3 个格**。
    为什么不照 `text.splitlines()` 数物理行：热记忆里的条目是**一条物理行**
    （`parse_hot` 只认 `- ` 开头的行，续行会被丢掉），一条 400 余字的天气表
    在文件里就是一行——按物理行数它永远只有 1 行，"≥3 行同构表格"这条判据
    对真实数据就永远不触发（真实工作区 `memory.md:7` 正是这个形状，它当时
    靠长度线才被判掉）。所以按格数行：`| a | b | c |` 是一行三格的表；
    `a | b` 只是两个字段并列，不算表（那种写法不许被误杀）。
    """
    rows = 0
    for line in text.splitlines():
        cells = [cell for cell in line.split("|") if cell.strip()]
        if len(cells) >= 3:
            rows += 1
    return rows


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
    if rows >= 1:
        # 行数口径来自 `_table_rows`：一行里 ≥3 个格才算一行表。**热记忆的条目是
        # 一条物理行**，所以"一条里有没有一张表"就是这里的判据——真实工作区
        # `memory.md:7` 的 12 城市天气表是一行九格，按物理行数数它永远只有 1 行，
        # 靠 `remember_table_lines=3` 这条阈值永远判不掉它（它当时靠长度线才被
        # 判掉）。所以这一条不再拿阈值卡单行表；阈值仍是多行表格的线，也在
        # `limits.KNOBS` 里可覆盖（按工作区调判据不该改代码）。
        return f"含 {rows} 行同构表格（≥{target.table_lines} 行即一次性产物）"
    if _CODE_FENCE.search(target.text):
        # **任意位置**的围栏都算：热记忆的条目是单行文本，围栏与它同行
        # （真实工作区 `memory.md:9/10` 就是这样）。原先只认行首，于是
        # 同一段交付正文换个排版就漏过去了。
        return "含代码围栏：交付正文不是一句话事实"
    for prefix in DELIVERY_PREFIXES:
        if target.text.startswith(prefix):
            return f"以交付模板开头「{prefix}」：一次性交付答复"
    hit = _DELIVERY_MARKER.search(target.text)
    if hit:
        return f"含交付模板小标题「**{hit.group(1)}」：某次交付的正文"
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


# ---- 去重指纹（设计稿 §4.3 / §5.3 ④ 第 1 级：精确级）----
#
# 口语化记忆的一句寒暄可以写成「你好！」「你好!」「 你好 ，」，**字符不同、
# 事实相同**——逐字相等挡不住它们，人工逐条比对也挡不住。所以「是不是同一条」
# 由**归一化后的指纹**回答：先按一套写明了的口径把文本压成比较用的形状，
# 再取 sha1 前若干位。口径本身是这个模块的公开接口（`NormalizePolicy`），
# 四个开关的值从 `limits` 取、可被 `.agent/limits.json` 覆盖——按工作区调一版
# 去重口径不该改代码（本仓既有规矩，设计稿 §4.3）。
#
# **只做结构性去重。** 这一级只认「归一化后逐字相同」，抓不到近似改写：
# 「用 uv 管理依赖」与「本仓统一用 uv」在它眼里是两条，编辑距离/Jaccard
# （`dedupe_similarity`）与冲突检测属 P1（设计稿 §5.3 ④ 第 2 级与 ⑤）——
# 这里**不引嵌入、不设相似度阈值**，也**不假装**做到了语义去重。
#
# 另外两条纪律：
#
# - **归一化只用于比较，不改写正文**：`judge` 与所有落点看到的仍是原文
#   （去标点后的「已确认」不再是 `ONESHOT_MARKERS` 里的「请确认」，
#   拿指纹当判据输入会把判据悄悄改掉）。
# - **归一化不动 CJK**：全角转半角只覆盖 U+FF01..U+FF5E 这一档（标准里与
#   ASCII 可打印字符一一对应），CJK 标点（`（`/`「`/`，`）逐个映射；汉字与
#   假名原样保留——不这样划界，韩文半角字母 U+FFA0..U+FFDC 也会被卷进来。

# 归一化口径里唯一的定点数：全角（U+FF01..U+FF5E）与 ASCII 可打印字符
# （U+0021..U+007E）之间的固定偏移，Unicode 标准定的，不是可调参数。
_FULLWIDTH_OFFSET = 0xFF01 - 0x21
# U+FF5F（半角左白括号）之后不再一一对应：只转 ASCII 那一档。
_FULLWIDTH_ASCII_END = 0xFF5E

# 中英文标点之间的映射。ASCII 标点走上面的全角规则，这里收 CJK 侧那几种
# **分类上是字母/符号、语义上是标点**的字符。
_PUNCT_MAP = {
    "，": ",",
    "。": ".",
    "、": ",",
    "：": ":",
    "；": ";",
    "？": "?",
    "！": "!",
    "（": "(",
    "）": ")",
    "《": "<",
    "》": ">",
    "「": '"',
    "」": '"',
    "『": '"',
    "』": '"',
    "【": "[",
    "】": "]",
    "〔": "(",
    "〕": ")",
    "—": "-",
    "–": "-",
    "…": ".",
    "·": ".",
    "〃": '"',
}

# 「空白」的显式清单：Unicode 的 Zs 之外，零宽与软连字符也算——
# 它们肉眼不可见，却会让两条本该相同的文本各走一条。
_ZERO_WIDTH = frozenset("\u200b\u200c\u200d\u2060\ufeff\u00ad")


def _is_whitespace(ch: str) -> bool:
    return ch in _ZERO_WIDTH or unicodedata.category(ch) == "Zs"


def _is_punctuation(ch: str) -> bool:
    """标点/符号。用 Unicode 分类而不是手写字符表：手写表必然漏。"""
    if ch in _PUNCT_MAP:
        return True
    return unicodedata.category(ch)[0] in ("P", "S")


def _to_halfwidth(ch: str) -> str:
    """全角转半角（只覆盖 ASCII 那一档）。"""
    code = ord(ch)
    if 0xFF01 <= code <= _FULLWIDTH_ASCII_END:
        return chr(code - _FULLWIDTH_OFFSET)
    return _PUNCT_MAP.get(ch, ch)


@dataclass(frozen=True)
class NormalizePolicy:
    """归一化口径：指纹比较时的四件事做不做。

    默认值就是设计稿 §4.3 的定案（去空白 + 去标点 + 全角转半角 + 小写），
    但每一个都来自 `limits` 的登记项，`.agent/limits.json` 可以覆盖。
    """

    casefold: bool
    collapse_ws: bool
    strip_punct: bool
    hash_chars: int


def normalize_policy(
    overrides: dict | None = None,
) -> NormalizePolicy:
    """取出当前生效的归一化口径（登记表的默认值 + 工作区覆盖）。"""

    def flag(name: str) -> bool:
        return bool(int(limits.resolve(name, overrides)[0]))

    return NormalizePolicy(
        casefold=flag("dedup_casefold"),
        collapse_ws=flag("dedup_collapse_ws"),
        strip_punct=flag("dedup_strip_punct"),
        hash_chars=int(limits.resolve("dedup_hash_chars", overrides)[0]),
    )


def normalize_key(
    text: str, overrides: dict | None = None, policy: NormalizePolicy | None = None
) -> str:
    """把文本压成**比较用**的形状（比较与指纹的唯一定义处）。

    口径（设计稿 §4.3 的原话：去空白、去标点、全角转半角、小写），
    四件事是否参与由 `NormalizePolicy` 决定，本函数不写死任何一条：

    1. 全角转半角：`（`→`(`、`！`→`!`、全角字母数字同理（这一步总是做，
       它是标点判定的前置——不做它，全角标点会被分类成符号而不是对标点）；
    2. 空白：`collapse_ws=1`（默认）时**整体去掉**（含 Tab/换行/全角空格/
       零宽字符）；`=0` 时折成一个半角空格并去掉首尾（用来区分「a b」与「ab」）；
    3. 标点：默认去掉（Unicode 分类 P/S 加 CJK 标点映射）；
    4. 大小写：默认 `casefold`。

    返回的字符串**不落库、不落盘**，只用来算指纹与判等。
    """
    if policy is None:
        policy = normalize_policy(overrides)

    out: list[str] = []
    for ch in text:
        half = _to_halfwidth(ch)
        if _is_whitespace(half):
            if not policy.collapse_ws:
                out.append(" ")
            continue
        if policy.strip_punct and _is_punctuation(half):
            continue
        out.append(half)
    key = "".join(out)
    if policy.casefold:
        key = key.casefold()
    if not policy.collapse_ws:
        # 折成空格之后，首尾与连续空白不该影响判等。
        key = " ".join(key.split())
    return key


def content_hash(
    text: str, overrides: dict | None = None, policy: NormalizePolicy | None = None
) -> str:
    """去重指纹：`sha1(normalize_key(text))` 的前 `dedup_hash_chars` 位。

    位数从登记表取（默认 16 = 64 bit，设计稿 §4.3 定案），不是写死的魔法数。
    空白串（归一化后为空）返回空指纹——调用方拿它判「这条没有可比内容」，
    不要让它和一条真的空条目互相去重。
    """
    if policy is None:
        policy = normalize_policy(overrides)
    key = normalize_key(text, policy=policy)
    if not key:
        return ""
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return digest[: policy.hash_chars]


def sanitize(text: str) -> tuple[str, list[str]]:
    """写入前的脱敏（设计稿 §5.3 ①）。

    与读路径共用 `transcript.redact_secrets` 的同一份形状表：写入侧再抄一遍正则，
    迟早两份会不一致。返回（脱敏后的文本，命中的形状名）——形状名可以写进理由，
    **值本身不回显**（理由里带上密钥值等于把它又抄了一遍）。
    """
    return redact_secrets_report(text)
