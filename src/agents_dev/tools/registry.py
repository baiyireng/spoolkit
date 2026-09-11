"""工具注册表与参数校验。

所有工具调用都必须经 invoke 进入，参数校验失败会被拦在这里，
避免模型编造的参数直接进入真实文件系统或命令行。
校验覆盖本项目实际使用的 JSON Schema 子集：type / required /
properties / additionalProperties / enum。不使用完整实现，以免引入依赖。
"""

from typing import Any

from agents_dev.tools.types import ToolCall, ToolResult, ToolSpec

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


def _validate(args: dict[str, Any], schema: dict[str, Any]) -> str | None:
    """校验通过返回 None，否则返回中文错误说明。"""
    if not isinstance(args, dict):
        return "参数必须是 JSON 对象"

    properties = schema.get("properties", {})

    if schema.get("additionalProperties", True) is False:
        unknown = set(args) - set(properties)
        if unknown:
            # 必须带上可用参数：只说「有个参数不认识」，模型唯一能做的就是
            # 猜。实测里它会在同一个调用上连续撞四次，把预算烧光——
            # 小模型的上下文经不起这种消耗。
            #
            # 带上每个参数是干什么的：工具之间的参数名有重叠（path / pattern），
            # 实测模型会把 A 工具的写法搬到 B 工具上，只报名字它仍然对不上号。
            allowed = "、".join(_describe(key, rule) for key, rule in sorted(properties.items()))
            if not allowed:
                allowed = "（本工具不接受参数）"
            return f"存在未知参数: {', '.join(sorted(unknown))}；可用参数: {allowed}"

    for key in schema.get("required", []):
        if key not in args:
            return f"缺少必填参数: {key}"

    for key, value in args.items():
        rule = properties.get(key)
        if rule is None:
            continue
        expected = _TYPE_MAP.get(rule.get("type", ""))
        if expected is not None and not isinstance(value, expected):
            return f"参数 {key} 类型应为 {rule['type']}"
        if "enum" in rule and value not in rule["enum"]:
            return f"参数 {key} 取值不在允许范围内"

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

        error = _validate(call.arguments, spec.parameters)
        if error is not None:
            return ToolResult(ok=False, content=f"参数校验失败: {error}")

        try:
            return spec.handler(dict(call.arguments))
        except Exception as exc:  # 工具内部异常不应中断主循环
            return ToolResult(ok=False, content=f"工具执行异常: {exc}")

    def describe(self) -> str:
        """生成紧凑的工具说明，供提示词使用。"""
        lines: list[str] = []
        for name in self.names():
            spec = self._specs[name]
            params = ", ".join(spec.parameters.get("properties", {}))
            lines.append(f"- {name}({params}): {spec.description}")
        return "\n".join(lines)

