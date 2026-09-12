"""按需展开工具说明。

每轮提示词里常驻的只有工具**索引**（名字 + 参数名 + 一句干什么），完整的
描述与参数含义放在这里按需取。分层是量出来的：十六个工具的完整清单
510 token，每个请求都要重述一遍，而一批 50 道回归任务里真正用到的只有 6 个。

**为什么不干脆让模型自己记住**：它会记错。实测它会把一个工具的参数搬到
另一个工具上，然后连着撞几次参数校验——每次撞都要花掉一轮预算。把参数表
放在手边（而不是让它猜）换来的正是这些省下来的轮次。

名字和分类都能查：`tool_help("dir_stats")` 给一个工具，`tool_help("看")`
给一类，`tool_help("*")` 给全部（慎用，那是把索引又摊平成清单）。
"""

from agents_dev.tools.registry import OTHER_GROUP, ToolRegistry
from agents_dev.tools.types import ToolResult, ToolSpec


def tool_help_spec(registry: ToolRegistry) -> ToolSpec:
    """绑定到某个注册表。

    绑定而不是全局：子智能体看到的是**它自己那一套**——给审查者列出写工具的
    说明，等于告诉它一件它做不到的事。
    """

    def handler(args: dict) -> ToolResult:
        query = str(args.get("name") or "").strip()
        if not query:
            return ToolResult(
                ok=False, content='name 不能为空：给工具名、类名，或者 "*"'
            )
        if query == "*":
            names = registry.names()
        elif registry.get(query) is not None:
            names = (query,)
        else:
            names = registry.in_group(query)
            if not names:
                groups = "、".join(
                    sorted({registry.group_of(name) for name in registry.names()})
                )
                return ToolResult(
                    ok=False,
                    content=(
                        f"没有叫「{query}」的工具，也没有这一类。"
                        f"类别有：{groups}；工具名有：{'、'.join(registry.names())}"
                    ),
                )
        return ToolResult(
            ok=True, content="\n\n".join(registry.explain(name) for name in names)
        )

    return ToolSpec(
        name="tool_help",
        description=(
            "查看工具的完整说明与参数含义。参数给工具名（如 read_file）、"
            "类别名（看 / 改 / 跑 / 量 / 核 / 记），或者 \"*\" 要全部。"
            "拿不准某个工具怎么用、该带哪些参数时用它，不要靠猜。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "工具名、类别名或 *"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        handler=handler,
        brief="查工具的完整说明",
        group=OTHER_GROUP,
    )
