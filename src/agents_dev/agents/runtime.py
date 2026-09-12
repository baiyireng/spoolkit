"""子智能体运行时。

子智能体不是第二个模型，而是同一个网关上的**独立干净上下文**，配专属提示词
与受限工具集。因此不额外占用显存。

它的核心价值不是并行——单 GPU 下不会真正并行——而是：它是一个一次性容器。
上下文写满了可以直接丢弃重来，代价仅为重派一次任务；而主循环若上下文溢出，
代价是整个任务状态丢失。两者不对等，所以把会膨胀的工作放进可丢弃的容器里。

两条硬性规则：
1. 写者与验者必须是不同上下文。同一个智能体自己写、自己测、自己判定通过，
   测试就退化成自我确认。
2. 审查者的工具集里没有写权限。它物理上无法通过改代码来掩盖问题。
"""

from dataclasses import dataclass, field
from typing import Any

from agents_dev.agent.loop import AgentLoop, LoopResult
from agents_dev.config import Config
from agents_dev.llm.gateway import ModelGateway
from agents_dev.llm.tokenizer import TokenCounter
from agents_dev.tools.registry import ToolRegistry
from agents_dev.tools.help import tool_help_spec


@dataclass(frozen=True)
class Role:
    """一个子智能体角色。"""

    name: str
    system_prompt: str
    tools: tuple[str, ...]
    can_write: bool


IMPLEMENTER = Role(
    name="implementer",
    system_prompt=(
        "你负责实现一处具体改动。只做任务说明里要求的事，不要顺手重构其它代码。"
        "改动前先确认目标符号的引用方，避免改坏调用者。"
    ),
    tools=(
        "read_file",
        "list_dir",
        "search_code",
        "request_diagnosis",
        "read_diagnosis",
        "file_symbols",
        "find_symbol",
        "find_callers",
        "run_command",
        "write_file",
        "replace_lines",
    ),
    can_write=True,
)

REVIEWER = Role(
    name="reviewer",
    system_prompt=(
        "你负责独立审查一处改动。只依据需求描述、改动差异与测试结果判断，"
        "不要附和任何人的推理过程。重点看：是否满足了验收标准、是否引入了"
        "未预期的副作用、是否有更简单的做法。给出明确结论与理由。"
        # 实测：不写这一句，它会去试 grep / sed / ls / pwd / python -c——
        # 这些都不在白名单里，于是一整轮预算全花在被拒绝的命令上，
        # 审查永远得不出结论。工具本来就是够的，它只是没想到用它们。
        "搜代码用 search_code，看文件用 read_file，看符号用 find_symbol / "
        "find_callers，跑测试用 run_command 执行 pytest。"
        "不要用 grep、ls、sed、python -c 这类命令——它们不在白名单里，"
        "只会把预算浪费在被拒绝上。"
    ),
    # 注意这里没有写权限：审查者不可能通过改代码来掩盖问题。
    # 审查者能跑测试但不能写代码：独立验证要靠自己动手跑，而不是附和实现者。
    tools=(
        "read_file",
        "file_symbols",
        "find_symbol",
        "find_callers",
        "search_code",
        "request_diagnosis",
        "read_diagnosis",
        "run_command",
    ),
    can_write=False,
)

ROLES: dict[str, Role] = {role.name: role for role in (IMPLEMENTER, REVIEWER)}


@dataclass(frozen=True)
class TaskSpec:
    """派发给子智能体的任务说明。

    必须是结构化的。含糊的说明会让实现者产出无效结果，失败点从一处变成
    两处，而且很难判断是分派错了还是执行错了。
    """

    goal: str
    targets: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    acceptance: str = ""
    out_of_scope: tuple[str, ...] = ()
    artifact: str = ""

    def validate(self) -> str | None:
        """派发前的硬闸门：没有验收标准就不允许派发。

        这一条是刻意的。没有可执行的判断依据，实现者做到什么程度都算「完成」，
        审查者也无从判断，整条流水线会变成两个模型互相附和。
        """
        if not self.goal.strip():
            return "任务目标不能为空"
        if not self.acceptance.strip():
            return "缺少验收标准，不允许派发：没有可执行的判断依据，改动无法验收"
        return None

    def render(self) -> str:
        sections = [f"任务目标：{self.goal}"]
        if self.targets:
            sections.append("涉及文件或符号：" + "、".join(self.targets))
        if self.constraints:
            sections.append("必须遵守的约束：" + "；".join(self.constraints))
        sections.append(f"验收标准：{self.acceptance}")
        if self.out_of_scope:
            sections.append("明确不要做的事：" + "；".join(self.out_of_scope))
        if self.artifact:
            sections.append(f"待审查的改动：\n{self.artifact}")
        return "\n".join(sections)


def restrict(registry: ToolRegistry, role: Role) -> ToolRegistry:
    """按角色裁剪工具集。这是权限的执行点，不是提示词里的君子协定。"""
    limited = ToolRegistry()
    for name in role.tools:
        # tool_help 不走这条路：它必须绑定到**裁剪之后**的注册表，
        # 否则审查者能查到一个它做不到的写工具，然后去尝试它。
        if name == "tool_help":
            continue
        spec = registry.get(name)
        if spec is not None:
            limited.register(spec)
    if registry.get("tool_help") is not None:
        limited.register(tool_help_spec(limited))
    return limited


def run_role(
    role: Role,
    spec: TaskSpec,
    gateway: ModelGateway,
    tokenizer: TokenCounter,
    registry: ToolRegistry,
    config: Config,
    max_steps: int | None = None,
    verify: Any | None = None,
) -> LoopResult:
    """在一个独立干净上下文里运行一个角色。

    不接预取、不接记忆：子智能体拿到的全部信息就是任务说明，
    这正是「视角独立」的实现方式——它不知道主循环的推理过程。
    """
    problem = spec.validate()
    if problem is not None:
        raise ValueError(problem)

    # 默认用配置里的子智能体预算，而不是主循环的预算：
    # 它们的成本结构不同，不该共用一个数字。
    limit = config.subagent_steps if max_steps is None else max_steps

    loop = AgentLoop(
        gateway=gateway,
        tokenizer=tokenizer,
        registry=restrict(registry, role),
        config=Config(
            project_root=config.project_root,
            context_window=config.context_window,
            max_steps=limit,
            subagent_steps=config.subagent_steps,
            # 督导的设置跟着整个运行走，而不是每个角色各自默认：
            # 父级关了督导、子智能体却还在自动续期，是最难查的那类不一致。
            supervise=config.supervise,
            step_ceiling=config.step_ceiling,
        ),
        prefetch=None,
        memory=None,
        persona=role.system_prompt,
        verify=verify,
    )
    return loop.run(spec.render())
