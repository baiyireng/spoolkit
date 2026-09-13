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

from spoolkit.agent.loop import AgentLoop, LoopResult
from spoolkit.config import Config
from spoolkit.llm.gateway import ModelGateway
from spoolkit.llm.tokenizer import TokenCounter
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.help import tool_help_spec

# 子智能体判断「这件事我独立做不完」时，final 用这个标记开头。
#
# 这条通道是**必需**的，不是礼貌用语：实测一次批量派发里，实现者撞上
# 「连续 10 次重复调用 write_file」的保护而收手，留下的信号是一句无从
# 行动的自述；而主循环需要知道的是「这块对它太大，拆小再派」。
# 不写这个标记，主循环只能从「结果不好」里去猜原因。
TOO_BIG_MARKER = "【太大】"


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
        # 开工前先估规模：这是「任务太大就早说」那条通道的入口。实测不写
        # 这句，它会硬做到底——烧光预算、撞上重复保护，回来只剩一句主循环
        # 无从行动的话。早说一句的价值在于**主循环还来得及拆**。
        "开工前先花一步估一估：这件事要读几个文件、改几处、大约几步。"
        "如果你的步数预算（{limit} 步）明显不够，不要硬做——直接把 done 设为 "
        f"true，final 以 {TOO_BIG_MARKER} 开头，写清为什么做不完，"
        "并给出一种可行的拆法（拆成哪几块、先做哪一块）。"
        "硬做只会烧光预算、留下半成品，比早说更糟。"
    ),
    tools=(
        "read_file",
        "list_dir",
        "search_code",
        "survey",
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
        # 审查也要先估规模：实测有一次被派去「审 50 道题的状态」，
        # 它在里面翻了半天，交回的结论没有意义。审不完也应当早说。
        "开工前先估一估这份审查有多大。如果一轮（{limit} 步）明显审不完，"
        f"直接 done 设为 true，final 以 {TOO_BIG_MARKER} 开头，"
        "说明要审的范围有多大、建议怎么拆。"
    ),
    # 注意这里没有写权限：审查者不可能通过改代码来掩盖问题。
    # 审查者能跑测试但不能写代码：独立验证要靠自己动手跑，而不是附和实现者。
    tools=(
        "read_file",
        "survey",
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
            session=config.session,
        ),
        prefetch=None,
        memory=None,
        # 角色提示词里的 {limit} 换成它这一轮真正拿到的步数：不写数字的话，
        # 它会拿主循环的预算来估自己的活。
        persona=role.system_prompt.replace("{limit}", str(limit)),
        verify=verify,
    )
    # 检查点用**角色自己的 id**：主循环和子智能体跑在同一个工作区里，
    # 共用 task.json 会让子智能体的状态盖掉主循环的进度——实测长任务里
    # 派发一次，主循环的检查点就变成了子智能体的状态。崩溃或续跑时，
    # 读到的是别人的进度。
    return loop.run(spec.render(), task_id=f"role-{role.name}")
