"""写入门禁：什么内容才配进热记忆。

设计稿 §5.1 把「什么值得记」写成五条判据（J1..J5）。这些判据必须是**代码**，
而不是提示词里的一句约束：实测证据是同一个模型产出的三条逐字相同的寒暄
（真实工作区 `memory.md:4/5/8`，设计稿 §2.5）——提示词约束对它无效，
两条重复的条目照样能落进每轮注入的热记忆。

P0 只实现了 J1/J3/J4，以及 J5 的「lesson 必须有触发词」；**J2（可核对）由 P1 的
T7 在本模块落地**（`_j2` + `judge(evidence=…)`）。J2 的输入列 `evidence` 只在
新表 `semantic` 上有（设计稿 §4.3），所以它的默认值是 `None`，意思是
「**这一列不存在，本判据不参与**」——旧 `memory` 表那条判据路径（`archive.py`
的 promote 循环、`cli/commands/memory.py` 的预演）照旧一字不改地跑；
`evidence=""`（调用方给了这一列、但没给值）才判「缺出处」。
`scope` 的必填仍属蒸馏 schema 的事（设计稿 §5.2），本模块不假装判过。

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
    """一次判定要看的东西。

    `evidence` 三态（J2，设计稿 §5.1 第 2 行）：
    `None` = 调用方没有这一列（旧 `memory` 表）→ J2 不参与；
    `""` = 有这一列但没给值 → 缺出处，拒；
    非空 = 还要过一遍「看得出是可核对的出处」的形状检查。
    """

    kind: str
    text: str
    trigger: str
    max_chars: int
    table_lines: int
    evidence: str | None = None


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


# ---- J2「可核对」（P1 / T7 落地；设计稿 §5.1 第 2 行 + §5.3 ⑥）----------------
#
# 反例是「无出处的断言、模型的自我描述」：这类话进了 `semantic` 就再也核不动，
# 而 `evidence` 正是设计稿 §4.3 点名的「最难作假的一列」。判据是**两段机械检查**
# （§5.1 的原话：`evidence` 非空，且能指到工作区内的文件/命令/工具输出）：
#
# 1. 去空白后为空 → 缺出处（这一条是硬线，没有例外）；
# 2. 非空但要**看得出形状**：file:line / 相对路径 / `工具名(` / 命令前缀 /
#    工具调用里带 `--flag` / `ep#12`、`semantic:12` 这类引用名。
#
# 这条形状表是**开源集**：允许的形状写在这里，配不上的给一句写明「要什么形状」
# 的理由（理由里**不回显 evidence**——它可能是半截密钥，回显等于把值又抄进
# audit 与账本）。为什么不要求「路径真的存在」：判据跑在写入侧，出处常是
# 命令输出与工具调用（`pytest -q tests/memory`、`read(src/...)`），它们在
# 工作区里没有对应的文件——那种"必须落盘存在"的检查会把真事实误杀，
# P0 的 J4 已经踩过一次「规则过宽/过严」的坑（§5.1 的风险注）。
#
# **过严的兜底口子**：形状表配不上但确实是可核对出处的，改这里的形状表，
# 别在每个调用点各写一套判据（判据只许有这一份）。
_EVIDENCE_FILE_SUFFIXES = (
    "py", "md", "json", "jsonl", "toml", "cfg", "ini", "txt", "log", "csv",
    "tsv", "yml", "yaml", "sql", "sh", "ps1", "bat", "js", "ts", "tsx", "jsx",
    "html", "css", "xml", "rst", "lock", "db", "whl", "exe", "patch", "diff",
)
# `store.py:105` / `docs/.../plan.md` / `说明.md`：扩展名在白名单里才算文件。
_EVIDENCE_FILE = re.compile(
    r"[\w.-]*\.(?:" + "|".join(_EVIDENCE_FILE_SUFFIXES) + r")(?::\d+)?\b",
    re.IGNORECASE,
)
# 相对路径（两侧都限 ASCII，免得把中文散文里的 `/` 当成路径）。
_EVIDENCE_PATH = re.compile(r"[A-Za-z0-9_.-]+[/\\][A-Za-z0-9_./\\-]*")
# `read(...)` / `rg（...）`：工具名是 ASCII 标识符（本仓的工具名都是），
# 后面跟半角或全角左括号。要求 ASCII 开头是为了不把「依据（见上）」这类
# 纯中文加括号的散文误当成工具调用。
_EVIDENCE_TOOL_CALL = re.compile(r"[A-Za-z_][\w.]*\s*[（(]")
# 命令前缀：**首词**是下面这些才认（`pytest -q`、`uv run pytest`、`git log -1`）。
_EVIDENCE_COMMAND_HEADS = frozenset(
    {
        "pytest", "python", "python3", "pip", "uv", "poetry", "hatch", "tox",
        "ruff", "mypy", "black", "git", "rg", "grep", "sed", "awk", "find",
        "spool", "npm", "pnpm", "yarn", "node", "npx", "make", "cargo", "go",
        "docker", "curl", "sqlite3", "powershell", "pwsh", "bash", "sh",
        "ls", "cat", "echo", "type", "where", "taskkill", "gh", "wsl",
    }
)
# `$ pytest -q` / `> uv run pytest`：命令提示符开头的也算。
_EVIDENCE_PROMPT = re.compile(r"^[>$]\s*\S")
# 带命令行开关的形状（`--frozen`、` -q`）：开关是命令的证据，散文里不会有。
_EVIDENCE_FLAG = re.compile(r"(?:^|\s)--?[A-Za-z][\w-]*")
# 库内引用名（设计稿 §5.4：`kind:id`）：`semantic:12` / `ep#12` / `lesson:7`。
_EVIDENCE_REF = re.compile(
    r"\b(?:ep|episode|semantic|memory|fact|preference|decision|lesson|skill)"
    r"[#:]\d+\b"
)


def _looks_checkable(evidence: str) -> bool:
    """`evidence` 能不能指到工作区里的文件 / 命令 / 工具输出（J2 的第二段检查）。"""
    stripped = evidence.strip()
    head = stripped.split()[0].lower() if stripped.split() else ""
    return bool(
        _EVIDENCE_FILE.search(stripped)
        or _EVIDENCE_PATH.search(stripped)
        or _EVIDENCE_TOOL_CALL.search(stripped)
        or _EVIDENCE_PROMPT.match(stripped)
        or _EVIDENCE_REF.search(stripped)
        or head in _EVIDENCE_COMMAND_HEADS
        or _EVIDENCE_FLAG.search(stripped)
    )


def _j2(target: _Target) -> str:
    if target.evidence is None:
        # 这一列不存在（旧 `memory` 表）：判据不参与，也不假装判过。
        return ""
    if not target.evidence.strip():
        return "缺出处：evidence 为空、无从核对，要 file:line / 命令 / 工具输出摘要"
    if not _looks_checkable(target.evidence):
        return (
            "evidence 看不出可核对的出处：要 file:line / 工作区内路径 / 命令 / "
            "工具输出摘要这样的形状（形状表见 gate._looks_checkable）"
        )
    return ""


# 判据表。顺序即判定顺序：被多条命中时以靠前的理由为准。
#
# J2 排在最后是**有意的**：它管的是"这条凭什么可信"，而 J1/J3/J4/J5 管的是
# "这条值不值得记"。一句寒暄即使没给 evidence，该说的也是「它是寒暄」——
# 理由要落在人最能改的那一条上。P0 的既有理由（J1/J3/J4/J5）因此一条不变。
RULES: tuple[Rule, ...] = (
    Rule("J1", "跨任务可复用", _j1),
    Rule("J3", "非寒暄/非自述", _j3),
    Rule("J4", "非一次性产物", _j4),
    Rule("J5", "有明确触发", _j5),
    Rule("J2", "可核对", _j2),
)

RULES_BY_ID: dict[str, Rule] = {rule.id: rule for rule in RULES}


def judge(
    kind: str,
    text: str,
    trigger: str = "",
    *,
    evidence: str | None = None,
    overrides: dict | None = None,
) -> Verdict:
    """判定一条候选条目能不能进热记忆（`semantic` 写入路径上是 J1–J5 五条）。

    `evidence` 的三态见 `_Target`：`None`（默认）= 这条路径没有 `evidence` 列，
    J2 不参与；`""` = 缺出处，J2 拒；非空 = 还要过「看得出可核对出处」的形状检查。
    默认值选 `None` 而不是 `""`，是为了让 P0 的两条既有调用点（`archive.py` 的
    promote 循环、`cli/commands/memory.py` 的预演）**行为一个字节不变**：
    它们判的是旧 `memory` 表的条目，那张表根本没有 `evidence` 列。

    `overrides` 与别处同一口径（`.agent/limits.json` 的覆盖值）：
    判据的阈值也必须能被按工作区覆盖，否则「调一版判据」就得改代码。
    """
    target = _Target(
        kind=kind,
        text=text.strip(),
        trigger=trigger.strip(),
        max_chars=int(limits.resolve("remember_max_chars", overrides)[0]),
        table_lines=int(limits.resolve("remember_table_lines", overrides)[0]),
        evidence=evidence,
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
