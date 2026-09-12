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


@dataclass(frozen=True)
class Claim:
    """交付物里的一处数字主张。"""

    text: str
    value: float
    unit: str
    line: int
    dimension: str
    canonical: float


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
                Claim(match.group(0), value, unit, index, dimension, canonical)
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


def untraceable(text: str, sources: list[str] | tuple[str, ...]) -> list[Claim]:
    """找出在工具输出里找不到出处的数字主张。"""
    origin = source_values(sources)
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


def render(claims: list[Claim]) -> str:
    if not claims:
        return "数字核对：交付物里的数字都能在工具输出里找到出处。"
    lines = ["数字核对：以下数字在本次运行的工具输出里**找不到出处**，请自行确认："]
    lines.extend(f"  - {claim.text}（第 {claim.line} 行）" for claim in claims)
    return "\n".join(lines)
