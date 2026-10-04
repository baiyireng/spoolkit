"""工具注册表与参数校验。

所有工具调用都必须经 invoke 进入，参数校验失败会被拦在这里，
避免模型编造的参数直接进入真实文件系统或命令行。
校验覆盖本项目实际使用的 JSON Schema 子集：type / required /
properties / additionalProperties / enum。不使用完整实现，以免引入依赖。
"""

import json
from typing import Any

from spoolkit.tools.types import NEXT_STEP_MARKER, ToolCall, ToolResult, ToolSpec

OTHER_GROUP = "其它"

# 索引里的分组与显示顺序。
#
# 分组不是为了好看：工具多起来之后，「十个名字加两个类别」比「二十个名字」
# 好扫得多；而且它让 `tool_help("看")` 这种按类展开成为可能——模型不必
# 先知道工具叫什么，才知道有哪些能力。
# 「外挂」是 MCP 服务提供的工具：它们不在本项目的工作区授权范围内，
# 单独成组是为了让模型和人一眼看出"这几个不是它自己的手"。
GROUP_ORDER = ("看", "查", "改", "跑", "量", "核", "诊", "记", "派", "外挂", OTHER_GROUP)

_TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _describe(key: str, rule: dict[str, Any]) -> str:
    """渲染一个参数：名字带上用途。用途来自 schema 里的 description。"""
    purpose = rule.get("description")
    return f"{key}（{purpose}）" if purpose else key


def _literal(value: Any) -> str:
    """把参数值写成**能照抄进调用里**的样子（JSON 字面量，中文不转义）。"""
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):  # pragma: no cover - 模型给不出这类值
        return '"…"'


def _fits(value: Any, rule: dict[str, Any]) -> bool:
    """这个值放得进这个参数吗（与校验同一套判据，不另立一份）。"""
    expected = _TYPE_MAP.get(rule.get("type", ""))
    if expected is not None and not isinstance(value, expected):
        return False
    return "enum" not in rule or value in rule["enum"]


def _placeholder(rule: dict[str, Any]) -> str:
    """必填但这次没给（或给错了）的参数，用它的类型写一个占位值。"""
    if "enum" in rule:
        return _literal(rule["enum"][0])
    return {
        "integer": "0",
        "number": "0",
        "boolean": "false",
        "array": "[]",
        "object": "{}",
    }.get(rule.get("type", ""), '"…"')


def _call_shape(
    name: str,
    properties: dict[str, Any],
    required: list[str],
    args: dict[str, Any],
    wrong: set[str],
) -> str:
    """这次调用**改对之后**的形状，模型可以整段照抄。

    三条规则，都是为了「照抄之后真的能跑」：

    - 这次给对的值**原样留着**（不是让它重填一遍，那等于把参数含义再讲一次）；
    - 不认识的、类型不对的**去掉或换成占位符**（`search_code(pattern=…,
      source=…)` → `search_code(pattern=…)`：`source` 不属于这个工具）；
    - 必填而这次没给的换成占位符（缺哪个补哪个，一眼看得出还差什么）。
    """
    parts: list[str] = []
    for key, rule in properties.items():
        if key in args and key not in wrong and _fits(args[key], rule):
            parts.append(f"{key}={_literal(args[key])}")
        elif key in required:
            parts.append(f"{key}={_placeholder(rule)}")
    return f"{name}({', '.join(parts)})"


def _reject(
    problem: str,
    name: str,
    properties: dict[str, Any],
    required: list[str],
    args: dict[str, Any],
    wrong: set[str] | None = None,
) -> str:
    """把一次参数校验失败渲染成**能照着改**的话术。

    三样都必须给，缺哪一样都会让模型原地打转：

    1. 错在哪（未知参数 / 缺必填 / 类型不对）；
    2. 这个工具**支持哪些参数、各自是干什么的**——工具之间的参数名有重叠
       （path / pattern / query），只说「不认识 source」，模型下一轮很可能
       再搬一个别的工具的参数过来。实测 `search_code` 带 `source` 连发三次，
       被拦两次后放弃，任务就停在那儿了；
    3. 一个**把这次调用改对之后的形状**。这是这一版新增的那一样：只列参数
       仍然要模型自己拼一次，而它拼错的概率正是刚才那三次。
       这一行以 `NEXT_STEP_MARKER` 开头是有意的——循环硬拦「原样重提」时
       认的就是这个前缀，会把**修正后的形状**原样端回去（`loop.py` 的
       `_retry_block_message`），所以「第一次被拦」与「重提被硬拦」两处
       给的是同一句话。
    """
    allowed = "、".join(
        _describe(key, rule) for key, rule in sorted(properties.items())
    ) or "（本工具不接受参数）"
    shape = _call_shape(name, properties, required, args, wrong or set())
    return "\n".join(
        (
            problem,
            f"可用参数: {allowed}",
            f"{NEXT_STEP_MARKER}把这次调用改成这个形状再发一次：{shape}",
        )
    )


def _validate(
    args: dict[str, Any], schema: dict[str, Any], name: str = "工具"
) -> str | None:
    """校验通过返回 None，否则返回中文错误说明（含可用参数与照抄形状）。"""
    if not isinstance(args, dict):
        return "参数必须是 JSON 对象"

    properties = schema.get("properties", {})
    required = list(schema.get("required", ()))

    if schema.get("additionalProperties", True) is False:
        unknown = sorted(set(args) - set(properties))
        if unknown:
            # 必须带上可用参数与照抄形状：只说「有个参数不认识」，模型唯一能做
            # 的就是猜。实测里它会在同一个调用上连续撞四次，把预算烧光——
            # 小模型的上下文经不起这种消耗。
            #
            # 带上每个参数是干什么的：工具之间的参数名有重叠（path / pattern），
            # 实测模型会把 A 工具的写法搬到 B 工具上，只报名字它仍然对不上号。
            return _reject(
                f"存在未知参数: {', '.join(unknown)}",
                name,
                properties,
                required,
                args,
                wrong=set(unknown),
            )

    for key in required:
        if key not in args:
            return _reject(
                f"缺少必填参数: {key}",
                name,
                properties,
                required,
                args,
            )

    for key, value in args.items():
        rule = properties.get(key)
        if rule is None:
            continue
        expected = _TYPE_MAP.get(rule.get("type", ""))
        if expected is not None and not isinstance(value, expected):
            return _reject(
                f"参数 {key} 类型应为 {rule['type']}（这次给的是 {type(value).__name__}）",
                name,
                properties,
                required,
                args,
                wrong={key},
            )
        if "enum" in rule and value not in rule["enum"]:
            choices = "、".join(_literal(one) for one in rule["enum"])
            return _reject(
                f"参数 {key} 取值不在允许范围内（只能是 {choices}）",
                name,
                properties,
                required,
                args,
                wrong={key},
            )

    return None


class ToolRegistry:
    """工具注册表。"""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        """注册一个工具，名称重复会直接报错（属配置错误）。"""
        if spec.name in self._specs:
            raise ValueError(f"工具名重复: {spec.name}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def invoke(self, call: ToolCall) -> ToolResult:
        """执行工具调用。任何失败都转成 ok=False 的结果。"""
        spec = self._specs.get(call.name)
        if spec is None:
            return ToolResult(ok=False, content=f"未知工具: {call.name}")

        error = _validate(call.arguments, spec.parameters, spec.name)
        if error is not None:
            return ToolResult(ok=False, content=f"参数校验失败: {error}")

        try:
            return spec.handler(dict(call.arguments))
        except Exception as exc:  # 工具内部异常不应中断主循环
            return ToolResult(ok=False, content=f"工具执行异常: {exc}")

    def describe(self) -> str:
        """生成工具的**索引**，供每轮提示词使用。

        只给「名字 + 参数名 + 一句干什么」，完整说明由 `explain` 按需给出。
        这个分层是量出来的：十六个工具的清单 510 token，而且**每个请求都要
        重述一遍**；同一批 50 道回归任务里真正用到的只有 6 个工具——
        没被用到的那些，每一轮都在收费。

        参数名必须留在索引里。语法约束给的是「所有工具参数的并集」，
        它不说哪个参数属于哪个工具；而模型在写下调用之前，参数表只有这一处
        可见。实测它会把一个工具的参数搬到另一个工具上，只给工具名它就只能猜。
        """
        buckets: dict[str, list[str]] = {}
        for name in self.names():
            spec = self._specs[name]
            group = spec.group or OTHER_GROUP
            buckets.setdefault(group, []).append(_index_entry(spec))

        ordered = [g for g in GROUP_ORDER if g in buckets]
        ordered += [g for g in buckets if g not in GROUP_ORDER]
        return "\n".join(f"- {g}：" + "；".join(buckets[g]) for g in ordered)

    def explain(self, name: str) -> str:
        """展开一个工具的完整说明：描述、参数含义、必填与选填。

        参数含义来自 schema 里的 description——提示词里省掉的那部分正是它，
        所谓「按需取」取的就是这个。
        """
        spec = self._specs.get(name)
        if spec is None:
            return f"没有叫 {name} 的工具"
        properties = spec.parameters.get("properties", {})
        required = set(spec.parameters.get("required", ()))
        lines = [f"{spec.name}（{spec.group or OTHER_GROUP}）：{spec.description}"]
        if properties:
            lines.append("参数：")
            for key, rule in properties.items():
                mark = "必填" if key in required else "选填"
                lines.append(f"  - {_describe(key, rule)}（{mark}）")
        else:
            lines.append("参数：无")
        return "\n".join(lines)

    def group_of(self, name: str) -> str:
        spec = self._specs.get(name)
        return (spec.group or OTHER_GROUP) if spec is not None else OTHER_GROUP

    def in_group(self, group: str) -> tuple[str, ...]:
        return tuple(name for name in self.names() if self.group_of(name) == group)


def _index_entry(spec: ToolSpec) -> str:
    params = ",".join(spec.parameters.get("properties", {}))
    brief = spec.brief or _first_clause(spec.description)
    head = f"{spec.name}({params})" if params else spec.name
    return f"{head} {brief}" if brief else head


def _first_clause(description: str, cap: int = 18) -> str:
    """没有 brief 时退回描述的第一句。

    宁可多花几个字，也不要让一个工具在索引里变成光秃秃的名字——名字
    说不出它是干什么的，而索引是模型唯一的线索。
    """
    text = description.strip()
    for mark in "。；：":
        cut = text.find(mark)
        if 0 < cut:
            text = text[:cut]
    return text[:cap] + "…" if len(text) > cap else text

