"""数字可追溯性检查。

交付物里出现的数字，要么来自工具输出、要么是程序算出来的。模型心算出来的
数不算来源——不是因为它算不对，而是因为**没有任何东西能核实它**。

实测踩过两次，方向还相反：把 5.1 GB 的目录说成 8 GB（往上编），把 7.6 GB
的 venv 说成 1.2 GB（拿其中一个 dll 的数当整体）。所以这不是「让它别瞎说」
能解决的——要求已经被反复证明无效，得靠制品本身可核对。

这一层只做**标注**，不阻断：它找出「在工具输出里找不到出处」的数字，
交给人和模型自己判断。把可疑当成错误会误伤（很多数字是合理的推导），
把错误当成正常才是真的危险。
"""

import re
from dataclasses import dataclass

# 只挑「带单位的量」：裸数字（序号、行号、版本号）噪声太大，
# 而带单位的数才是真正会被引用、也会被编造的那一类。
_UNIT = r"(?:GB|MB|KB|TB|B|个文件|个字节|个|%|token|tokens)"
_CLAIM = re.compile(rf"(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>{_UNIT})")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")

# 单位换算到同一个量纲再比。不换算的话，`390.3 MB` 与 `0.4 GB` 会被判成
# 两回事——而那是完全正确的引用（实测就这么误报过一次）。
_UNITS: dict[str, tuple[str, float]] = {
    "B": ("bytes", 1.0),
    "字节": ("bytes", 1.0),
    "个字节": ("bytes", 1.0),
    "KB": ("bytes", 1 << 10),
    "MB": ("bytes", 1 << 20),
    "GB": ("bytes", 1 << 30),
    "TB": ("bytes", 1 << 40),
    "个": ("count", 1.0),
    "个文件": ("count", 1.0),
    "token": ("count", 1.0),
    "tokens": ("count", 1.0),
    "%": ("percent", 1.0),
}

# 同单位照抄：1% 就够（13.10 与 13.1 是同一个数）。
SAME_UNIT_TOLERANCE = 0.01
# 换过单位：放宽到 10%。换算加上取整本来就会挪几个百分点——
# `390.3 MB -> 0.4 GB` 偏 4.9%，这是对的说法，不该被当成编造。
# 而真正的编造（5.1 GB 说成 8 GB）偏 57%，怎么放宽也拦得住。
CONVERTED_TOLERANCE = 0.10

# 「这是它的推算」的标志。两种来源，都说明模型自己知道这个数不是量出来的：
#   1. 不确定的语气——约 / 预估 / 通常 / 左右 / 给的是个区间；
#   2. 预测的量——「可释放 30 GB」量的是「做了这件事能省多少」，
#      而不是「现在是多少」。这一类天经地义没有出处。
# 判据是**整行**：推算的说法常常离数字十几字远（「通常占 Rust 项目体积的 80%」），
# 只盯数字左右几个字会漏掉，然后就把它当成引用报出去——实测噪声就是这么来的。
_ESTIMATE = re.compile(
    "|".join(
        (
            r"大约|大概|约莫|约(?!束|定)|预估|预计|预期|估计|估算|推测",
            # 「上下文」「前后端」是这个领域的高频词，别把它们当成约数。
            r"通常|一般|往往|至多|至少|左右|上下(?!文)|前后(?!端)|范围|区间",
            r"(?:可|能)(?:安全)?(?:释放|清理|回收|省下|省)",
            rf"\d[\d.]*\s*[-–~—]\s*\d[\d.]*\s*(?:{_UNIT})",
        )
    )
)


@dataclass(frozen=True)
class Claim:
    """交付物里的一处数字主张。"""

    text: str
    value: float
    unit: str
    line: int
    dimension: str
    canonical: float
    context: str = ""

    @property
    def is_estimate(self) -> bool:
        """这个数是不是它的推算，而不是引用。

        两类分开列，因为价值完全不同：**引用**没有出处是真问题——
        报告在声称「我量到的是这个」；而**推算**本来就没有出处，
        它是模型该做的判断（「删掉 target 大概能省多少」）。
        混在一起列，看的人会直接忽略整张单子。
        """
        return bool(_ESTIMATE.search(self.context))


def _to_float(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def _canonical(value: float, unit: str) -> tuple[str, float] | None:
    """换算到统一量纲。不认识单位就当没有。"""
    rule = _UNITS.get(unit)
    if rule is None:
        return None
    dimension, factor = rule
    return dimension, value * factor


def claims_in(text: str) -> list[Claim]:
    """把文本里带单位的数字主张抽出来。"""
    found: list[Claim] = []
    for index, line in enumerate(text.splitlines(), start=1):
        for match in _CLAIM.finditer(line):
            value = _to_float(match.group("num"))
            if value is None:
                continue
            unit = match.group("unit")
            converted = _canonical(value, unit)
            if converted is None:
                continue
            dimension, canonical = converted
            found.append(
                Claim(
                    match.group(0),
                    value,
                    unit,
                    index,
                    dimension,
                    canonical,
                    context=line,
                )
            )
    return found


def source_values(
    sources: list[str] | tuple[str, ...]
) -> dict[str, list[float]]:
    """工具输出里出现过的数，按量纲归类。

    带单位的按换算后的值收；不带单位的收进 count——`58119` 与 `58119 个`
    指的是同一个数，而计算器给的裸结果也只可能是这个形式。
    """
    values: dict[str, list[float]] = {"bytes": [], "count": [], "percent": []}
    for source in sources:
        for match in _CLAIM.finditer(source):
            value = _to_float(match.group("num"))
            if value is None:
                continue
            converted = _canonical(value, match.group("unit"))
            if converted is not None:
                values[converted[0]].append(converted[1])
        # 裸数字进 count：计算器的输出就是光秃秃一个数。
        stripped = _CLAIM.sub(" ", source)
        for raw in _NUMBER.findall(stripped):
            value = _to_float(raw)
            if value is not None:
                values["count"].append(value)
    return values


def _close(left: float, right: float, tolerance: float) -> bool:
    if abs(left - right) <= 0.01:
        return True
    return abs(left - right) <= abs(right) * tolerance


def untraceable(
    text: str, sources: list[str] | tuple[str, ...], facts: list | tuple = ()
) -> list[Claim]:
    """找出在工具输出里找不到出处的数字主张。

    出处分两处：工具**渲染出来的文本**，和它交出的**结构化事实**。
    后者不是锦上添花——渲染是掐过的（`dir_stats` 只列前 8 个子项），
    掐掉的那部分数在文本里找不到，于是报告里引用了真实数据的每一处
    都成了「找不到出处」。实测这么误报过 19 处。
    """
    origin = source_values(sources)
    for fact in facts or ():
        item = _canonical(fact.value, fact.unit)
        if item is not None:
            origin[item[0]].append(item[1])
    result = []
    for claim in claims_in(text):
        tolerance = (
            SAME_UNIT_TOLERANCE
            if claim.dimension == "count"
            else CONVERTED_TOLERANCE
        )
        if not any(
            _close(claim.canonical, value, tolerance)
            for value in origin.get(claim.dimension, ())
        ):
            result.append(claim)
    return result


def _split(claims: list[Claim]) -> tuple[list[Claim], list[Claim]]:
    """引用与推算，各归各的。"""
    return (
        [claim for claim in claims if not claim.is_estimate],
        [claim for claim in claims if claim.is_estimate],
    )


def render(claims: list[Claim], wrong: list[Claim] | None = None) -> str:
    """把两类问题分开说。

    分开是必要的：一类是「这个数根本没有出处」，另一类是「数有出处但配错了
    对象」。前者是编造，后者是错认——处置不同，混在一起会让人以为是一回事。
    """
    wrong = wrong or []
    # 引用与推算分开：前者是在声称「我量到的是这个」，没有出处才有问题；
    # 后者是它的估计，本来就没出处。混在一起列，看的人会忽略整张单子。
    cited, estimated = _split(claims)
    lines: list[str] = []
    if wrong:
        lines.append(
            "数字核对：以下数字**对不上它所在的那个对象**"
            "（这个数在别处找得到出处，像是挂错了地方）："
        )
        lines.extend(f"  - {claim.text}（第 {claim.line} 行）" for claim in wrong)
    if cited:
        lines.append("数字核对：以下数字**看起来是引用**，但工具输出里找不到出处：")
        lines.extend(f"  - {claim.text}（第 {claim.line} 行）" for claim in cited)
    if estimated:
        lines.append(
            f"数字核对：另有 {len(estimated)} 处数字是推算而不是测量"
            "（带了「约 / 预估 / 范围」这类字眼，或者量的是「清了能省多少」），"
            "不逐条列——它们本来就不会有出处，用的时候当成估计看。"
        )
    if not lines:
        return "数字核对：交付物里的数字都能在工具输出里找到出处。"
    return "\n".join(lines)


def summarize(claims: list[Claim], wrong: list[Claim] | None = None) -> str:
    """同一件事的一行说法——事件流里一行就是一行，展开的那版是 `render`。

    网页那边只显示一句话，把 35 个数字糊上去等于没说：看不清哪个是问题。
    所以这一行只说「有几处、分别是什么」，推算只报数量。
    """
    wrong = wrong or []
    cited, estimated = _split(claims)
    parts: list[str] = []
    if wrong:
        parts.append(f"配错对象 {len(wrong)} 处：" + "、".join(c.text for c in wrong))
    if cited:
        parts.append(f"找不到出处 {len(cited)} 处：" + "、".join(c.text for c in cited))
    if estimated:
        parts.append(f"另有推算 {len(estimated)} 处（推算本来就没有出处）")
    return "数字核对：" + "；".join(parts)


def actionable(claims: list[Claim], wrong: list[Claim] | None = None) -> bool:
    """有没有**值得看**的东西。只有推算时返回假——那是它该做的判断。"""
    cited, _ = _split(claims)
    return bool(wrong) or bool(cited)


def _normalize(text: str) -> str:
    return text.lower().replace("\\", "/")


def _is_subject_name(name: str) -> bool:
    """这个字符串够不够当主语。

    要有个字母。`.0` `.1` 这类扩展名会**撞进数字里**——`13.1 GB` 里的 `.1`
    会被当成一个对象，而且因为位置靠后，它还会盖掉真正的主语。
    """
    return len(name) >= 2 and any(char.isalpha() for char in name)


# 这一句里提到「清 / 释放 / 删」的话，跟在后面的数多半是「做了这件事能省多少」，
# 而不是「这个对象有多大」。前者是推算，没有出处可言，也不该判配错。
_PROJECTION = re.compile(r"清|释放|回收|省|删|腾")


def _clause_start(line: str, position: int) -> int:
    """这一句从哪儿开始。

    逗号不算断句：「若只清 models + 缓存，约 15 GB」是一句话——
    按逗号切开的话，「清」就被切到前一半去了。
    """
    start = 0
    for index, char in enumerate(line[:position]):
        if char in "。；！？":
            start = index + 1
    return start


def _values_in(items: list, dimension: str) -> list[float]:
    """这个对象在某一类量纲上的值。没有这一类，就轮不到它当主语。"""
    found: list[float] = []
    for fact in items:
        item = _canonical(fact.value, fact.unit)
        if item is not None and item[0] == dimension:
            found.append(item[1])
    return found


def mismatched(
    text: str, facts: list | tuple
) -> list[Claim]:
    """找出「数找得到出处，但挂错了对象」的数字。

    判据用**同一行里最近的前置主语**：散文里数字通常跟在它的对象后面，
    所以「`a\\b\\c` 约 8 GB」中的 8 属于 `c`，不属于更靠左的 `a`。
    「最近」按**结束位置**算，位置打平取更长的那个——`pyvideotrans` 与
    `.venv` 会结束在同一处（`.venv` 是它的尾巴），而 `.safetensors` 是
    `NoobAI-XL-v1.1.safetensors` 的尾巴：不取更长的那个，主语就会是
    那个「尾巴」，而尾巴的量（所有 .safetensors 的总和）跟眼前的数对不上，
    于是每一处都成了配错。这是实测里噪声的主要来源。

    两道关，缺一不报：
      1. 这个数**对不上**它所在的对象（结构化事实说 `c` 是 5.1）；
      2. 这个数**在别的对象那里找得到出处**（8.4 是 `a` 整体的）。

    第二道关是**宁可漏报也不误报**：只满足第一条的往往是模型的推算
    （「`c` 能安全清理约 2 GB」——2 GB 哪儿都对不上，但那是它的判断，
    不是抄错了对象）。加上第二条，报出来的话才立得住：这个数是从别人
    那儿搬过来的。实测那两处错误都满足两条。

    句子在说「清了能省多少」时整条跳过——那种量本来就是对未来的估算。

    这条判据能成立的前提是工具**交出了结构化事实**。只有渲染好的文本时，
    核对只能做到「这个数有没有出现过」——而实测里有一半的错误恰恰是
    「数出现过，但配到了别的对象上」。
    """
    if not facts:
        return []
    by_subject: dict[str, list] = {}
    for fact in facts:
        name = _normalize(fact.subject)
        if not _is_subject_name(name):
            continue
        by_subject.setdefault(name, []).append(fact)
    if not by_subject:
        return []

    wrong: list[Claim] = []
    for index, line in enumerate(text.splitlines(), start=1):
        lowered = _normalize(line)
        for match in _CLAIM.finditer(line):
            value = _to_float(match.group("num"))
            if value is None:
                continue
            converted = _canonical(value, match.group("unit"))
            if converted is None:
                continue
            dimension, canonical = converted
            tolerance = (
                SAME_UNIT_TOLERANCE if dimension == "count" else CONVERTED_TOLERANCE
            )

            # 「清了能省多少」不是「它有多大」。前者是推算，不判配错。
            if _PROJECTION.search(lowered[_clause_start(lowered, match.start()) : match.start()]):
                continue

            # 先找这个数前面最近的那个对象名——**任何量纲都算**，只是定「谁」。
            # 位置打平取更长的：`NoobAI-XL-v1.1.safetensors` 与 `.safetensors`
            # 结束在同一处，后者只是它的尾巴，不是主语。
            subject = ""
            best_end = -1
            for name in by_subject:
                position = lowered.rfind(name, 0, match.start())
                if position < 0:
                    continue
                end = position + len(name)
                if end > best_end or (end == best_end and len(name) > len(subject)):
                    best_end, subject = end, name
            if not subject:
                continue  # 没提到任何已知对象，就谈不上配错
            # 定了主语之后再问：它有没有**这一类的**数。没有就说明这个数说的是
            # 另一码事（「1 个文件」跟在文件名后面，而文件个数是目录的属性），
            # 不能退回去找更远的对象——那样找出来的主语不是它。
            expected = _values_in(by_subject[subject], dimension)
            if not expected:
                continue

            if any(_close(canonical, value, tolerance) for value in expected):
                continue  # 挂在自己的对象上，没问题
            # 只满足「对不上」还不够——那可能只是推算。要它**在别处有家**，
            # 才谈得上「挂错了地方」。
            if any(
                _close(canonical, value, tolerance)
                for name, items in by_subject.items()
                if name != subject
                for value in _values_in(items, dimension)
            ):
                wrong.append(
                    Claim(
                        match.group(0),
                        value,
                        match.group("unit"),
                        index,
                        dimension,
                        canonical,
                    )
                )
    return wrong
