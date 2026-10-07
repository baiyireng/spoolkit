"""素材压缩：把已经读到的素材压到装得下，**判据原文一个字都不掉**。

分两层，第二层只在第一层还不够小的时候才用：

1. **机械层**（零模型调用）：按对象去重（同一个对象的后一次读数才算数）、逐条
   截断，并且把**判据行**（失败/错误/断言/回溯/退出码）整行挑出来单独放——它们
   不截断、不改写、也不交给模型。
2. **模型层**（干净上下文）：只喂「问题 + 每条素材的调用声明 + 机械层压过的正文」，
   不给历史、不给最终答复、不给判据原文。模型输出回来再**逐行机械核对**：引用了
   素材里没有的判据行或文件名，那一行整行拦下；一行都没剩下就整段回退机械层结果。

为什么判据行要单独走一条通道，而不是交给模型压缩后再核对：核对只能拦住"编造
出来的行"，拦不住"悄悄漏掉的行"——漏掉是删改，不是新增，逐行核对看不出来。
真实教训就在反方向：26 条收集错误把唯一那条 `FAILED` 从提示里挤掉，模型于是
去"修"收集错误而不是找失败测试（`benchmarks/probes/orchestration/README-real.md`
的 S3 节）。**保护区逐字直通是结构性的，不靠模型自觉。**

这一层只负责"把素材变成一段更短的文本"；什么时候压、压完怎么装回上下文，由
`context/budget.compact_trigger` 判、由主循环在重置之前接（计划 S6）。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from spoolkit.context.budget import Budget, CompactDecision, compact_trigger
from spoolkit.llm.tokenizer import OfflineTokenCounter, TokenCounter

# --------------------------------------------------------------------------
# 保护区：命中这些记号的**整行**逐字保留
# --------------------------------------------------------------------------
#
# 只认机械可判的记号，不靠语义判断（"这行看起来像不像判据"这种判据自己就会漂）。
# 代价是保护得偏宽：读到一份测试源码时 `assert ` 之类的行也会被整行留下——宁可
# 少压一点，也不许把判据原文压坏。
JUDGEMENT_MARKERS: tuple[str, ...] = (
    "FAILED ",
    "ERROR ",
    "AssertionError",
    "Traceback (most recent call last)",
    "Error:",
    "Exception",
    "退出码",
    "exit code",
)

SECTION_HEAD = "【素材（已压缩；判据原文逐字保留）】"
PROTECTED_HEAD = "【判据原文（逐字，未改写）】"
BULK_HEAD = "【其余素材（已压缩）】"

#: 每条素材压到多少字符（机械层）。
BULK_ITEM_CHARS = 240

#: 机械层最多留几条（从最新的往回取）。
BULK_MAX_ITEMS = 12

#: 截断时尾部占的比例（头 2/3、尾 1/3）。工具的结论常常在末尾（失败要点、
#: 退出码、统计行），只留头会把最要紧的那几行切掉。
BULK_TAIL_SHARE = 3

PROMPT_HEAD = (
    "你在压缩一份「已经读到的素材」，让它更短但足以支撑结论。规矩：\n"
    "1. 只准删减与归类，**不许新增任何事实**：不许写素材里没有的文件名、行号、"
    "命令、数字、判据。\n"
    "2. 判据原文（失败/错误/断言/回溯/退出码）已经由机械层单独留好，不在你这份"
    "输入里，也不要凭印象补。\n"
    "3. 每行一条：`- [对象] 内容`；对象写下面「调用声明」里给出的名字。\n"
    "4. 不确定的一律不写。宁可少写，不许编。\n"
)

#: 看起来像「带扩展名的文件/路径」的词。用来拦模型编出来的文件名。
_PATH_RE = re.compile(r"[A-Za-z0-9_./\\-]+\.[A-Za-z0-9_]+")
_LABEL_RE = re.compile(r"^\s*[-*]\s*\[([^\]]+)\]\s*(.*)$")


def is_judgement(line: str) -> bool:
    """这一行是不是判据原文（命中任一记号即算）。"""
    return any(marker in line for marker in JUDGEMENT_MARKERS)


def material_label(tool: str, key: str) -> str:
    """素材每一行的抬头：要能看出这一条是**对哪个对象**做的。

    只写工具名时"素材还在"这句话不可核对：模型看得到一段正文，却不知道它是从
    哪个文件读来的——路径本身就是素材的一半。
    """
    raw = key.partition(" ")[2]
    try:
        args = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return tool
    if not isinstance(args, dict):
        return tool
    path = args.get("path")
    if isinstance(path, str) and path:
        return f"{tool} {path}"
    return tool


@dataclass(frozen=True)
class MaterialNote:
    """一条素材：对哪个对象做的一次调用，拿到了什么。"""

    key: str
    tool: str
    body: str
    why: str = ""

    @property
    def label(self) -> str:
        return material_label(self.tool, self.key)


def notes_from_material(
    material: Sequence[tuple[str, str, str]],
) -> list[MaterialNote]:
    """主循环那份 `(去重键, 工具名, 正文)` 记法转成本模块的记法。"""
    return [
        MaterialNote(key=key, tool=tool, body=body)
        for key, tool, body in material
    ]


def clip_body(text: str, *, room: int = BULK_ITEM_CHARS) -> str:
    """把一段正文截到 room 字符：留头 2/3、留尾 1/3，中间如实标出省略了多少。"""
    flat = " ".join(str(text).split())
    if len(flat) <= room:
        return flat
    if room <= 0:
        return ""
    head = room - room // BULK_TAIL_SHARE
    tail = room - head
    omitted = len(flat) - head - tail
    if omitted <= 0:
        return flat[:room]
    return f"{flat[:head]}…（中间省略 {omitted} 字符）…{flat[-tail:]}"


@dataclass(frozen=True)
class Mechanical:
    """机械层的结果：判据行（逐字）与非判据行（截断）分开装。"""

    protected: tuple[str, ...]
    bulk: tuple[str, ...]
    dropped_items: int

    @property
    def lines(self) -> tuple[str, ...]:
        return self.protected + self.bulk

    @property
    def text(self) -> str:
        parts = [SECTION_HEAD]
        if self.protected:
            parts.append(PROTECTED_HEAD)
            parts.extend(self.protected)
        if self.bulk:
            parts.append(BULK_HEAD)
            parts.extend(self.bulk)
        return "\n".join(parts) if (self.protected or self.bulk) else ""


def mechanical_compact(
    notes: Sequence[MaterialNote],
    *,
    item_chars: int = BULK_ITEM_CHARS,
    max_items: int = BULK_MAX_ITEMS,
) -> Mechanical:
    """零模型调用的那一层：去重 → 逐条截断 → 判据行整行留下。

    判据行不参与截断，也不受条数上限约束：它们是做判断的依据本身，压坏了这一层
    就没意义了。条数上限只约束普通正文（从最新的往回取）。
    """
    latest: dict[str, MaterialNote] = {}
    for note in notes:
        latest[note.key] = note  # 同一个对象以最后一次读数为准
    dropped = len(notes) - len(latest)
    kept = list(latest.values())[-max_items:] if max_items > 0 else []

    protected: list[str] = []
    bulk: list[str] = []
    for note in kept:
        label = note.label
        plain: list[str] = []
        for line in str(note.body).splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if is_judgement(stripped):
                protected.append(f"- [{label}] {stripped}")
            else:
                plain.append(stripped)
        if plain:
            bulk.append(f"- [{label}] {clip_body(' '.join(plain), room=item_chars)}")
    return Mechanical(tuple(protected), tuple(bulk), dropped)


def build_prompt(
    goal: str,
    notes: Sequence[MaterialNote],
    *,
    bulk_lines: Sequence[str],
    budget_tokens: int,
) -> str:
    """模型层的输入：**只有**问题、调用声明、机械层压过的正文。

    没有历史、没有最终答复、没有判据原文——这是"干净上下文"的具体含义，也是
    `tests/agent/test_material_compactor.py` 会直接断言的东西。
    """
    declarations = ["- [{}] {}".format(n.label, n.why or "（没有说明）") for n in notes]
    return (
        PROMPT_HEAD
        + "\n【问题】\n"
        + goal.strip()
        + "\n\n【调用声明】（每条素材是对哪个对象做的、当时为什么用它）\n"
        + ("\n".join(declarations) if declarations else "（没有）")
        + f"\n\n【待压缩正文】（压到 {budget_tokens} token 以内）\n"
        + ("\n".join(bulk_lines) if bulk_lines else "（没有）")
    )


def verify_model_output(
    text: str,
    *,
    source: str,
    allowed_labels: Sequence[str],
) -> tuple[list[str], int]:
    """逐行机械核对模型的输出，返回（留下的行, 拦下的行数）。

    三条规矩，全部机械可判：
    - 行必须是 `- [对象] 内容`，对象必须在调用声明里出现过（不许换对象）；
    - 判据行必须**逐字**出现在素材原文里（不许编造失败测试名）；
    - 行里像文件/路径的词必须在素材原文里出现过（不许编造文件名）。
    """
    labels = set(allowed_labels)
    kept: list[str] = []
    dropped = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _LABEL_RE.match(line)
        if match is None:
            dropped += 1
            continue
        label, body = match.group(1).strip(), match.group(2).strip()
        if label not in labels:
            dropped += 1
            continue
        if is_judgement(body) and body not in source:
            dropped += 1
            continue
        if any(token not in source for token in _PATH_RE.findall(body)):
            dropped += 1
            continue
        kept.append(f"- [{label}] {body}")
    return kept, dropped


@dataclass(frozen=True)
class CompactOutcome:
    """一次素材压缩的结果。"""

    text: str
    """要注入回上下文的文本；空串表示这一轮不用压（没到线）。"""

    decision: CompactDecision
    used_model: bool
    tokens: int
    kept_items: int
    dropped_items: int
    protected_lines: int
    dropped_lines: int = 0
    fallback: str = ""
    """非空＝模型层没用上（没给、调用失败、输出没变短、被拦空了），值是原因。"""

    seconds: float = 0.0
    over_budget: bool = False
    """压完仍然超过 `decision.keep_tokens`：如实记下，不假装压够了。"""


def compact_material(
    notes: Sequence[MaterialNote],
    *,
    goal: str,
    budget: Budget,
    used: int,
    model: Callable[[str], str] | None = None,
    counter: TokenCounter | None = None,
    item_chars: int = BULK_ITEM_CHARS,
    max_items: int = BULK_MAX_ITEMS,
) -> CompactOutcome:
    """按预算判定要不要压；要压就先机械层、还不够小再上模型层。

    `model` 是一个只接收 prompt、返回文本的干净上下文调用口（S6 用真实网关接），
    拿不到就只用机械层。**没到线时一次模型调用都不会发生**——这条是用例直接
    数调用次数断言的。
    """
    token_counter = counter or OfflineTokenCounter()
    raw = "\n".join(note.body for note in notes)
    material_tokens = token_counter.count(raw)
    decision = compact_trigger(budget, used=used, material=material_tokens)

    def nothing() -> CompactOutcome:
        return CompactOutcome(
            text="",
            decision=decision,
            used_model=False,
            tokens=0,
            kept_items=0,
            dropped_items=0,
            protected_lines=0,
        )

    if not decision.should:
        return nothing()

    mech = mechanical_compact(notes, item_chars=item_chars, max_items=max_items)
    mech_tokens = token_counter.count(mech.text)

    def mechanical_only(fallback: str, dropped_lines: int = 0) -> CompactOutcome:
        return CompactOutcome(
            text=mech.text,
            decision=decision,
            used_model=False,
            tokens=mech_tokens,
            kept_items=len(mech.bulk),
            dropped_items=mech.dropped_items,
            protected_lines=len(mech.protected),
            dropped_lines=dropped_lines,
            fallback=fallback,
            over_budget=mech_tokens > decision.keep_tokens,
        )

    if mech_tokens <= decision.keep_tokens:
        return mechanical_only("")
    if model is None:
        return mechanical_only("没给压缩模型，只用机械层")

    # 同一个模块里取标定值：`_knob` 是私有的，但它要的就是「含本次运行覆盖的值」。
    model_tokens = int(budget._knob("material_compact_model_tokens"))
    prompt = build_prompt(
        goal, notes, bulk_lines=mech.bulk, budget_tokens=model_tokens
    )
    started = time.time()
    try:
        produced = model(prompt)
    except Exception as error:  # noqa: BLE001 - 压缩失败不该打断整个运行
        return mechanical_only(
            f"压缩模型调用失败：{type(error).__name__}: {error}"
        )
    seconds = time.time() - started
    if not isinstance(produced, str) or not produced.strip():
        return mechanical_only("压缩模型没有给出内容")

    source = "\n".join(mech.lines)
    kept, dropped_lines = verify_model_output(
        produced, source=source, allowed_labels=[n.label for n in notes]
    )
    if not kept:
        return mechanical_only("压缩模型的输出一行都没通过核对", dropped_lines)
    body = "\n".join(kept)
    if token_counter.count(body) >= token_counter.count("\n".join(mech.bulk)):
        return mechanical_only("压缩模型的输出没有变短", dropped_lines)

    text = "\n".join([SECTION_HEAD, PROTECTED_HEAD, *mech.protected, BULK_HEAD, body])
    if not mech.protected:
        text = "\n".join([SECTION_HEAD, BULK_HEAD, body])
    tokens = token_counter.count(text)
    return CompactOutcome(
        text=text,
        decision=decision,
        used_model=True,
        tokens=tokens,
        kept_items=len(kept),
        dropped_items=mech.dropped_items,
        protected_lines=len(mech.protected),
        dropped_lines=dropped_lines,
        fallback="",
        seconds=seconds,
        over_budget=tokens > decision.keep_tokens,
    )
