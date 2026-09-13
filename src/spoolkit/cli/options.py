"""运行选项的决策。

把「命令行参数 + 已保存设置」翻译成实际生效的值。集中在一处，
是因为这类决策的默认值很危险——散落各处时，你很难回答
「现在到底允许自动落盘到哪」这个问题。
"""

import argparse
from pathlib import Path
from typing import Sequence

from spoolkit.llm.gateway import ModelGateway
from spoolkit.policy import (
    describe as describe_policy,
    load_policy,
    policy_path,
)

# 探测到的窗口**直接采用**，不再人为封顶。
#
# 这里原先写死过一个 32768 的上限，理由是「不封顶会让短上下文特化失去意义
# （配额永远用不完、截断永远不触发）」——那是为了**验证我们自己的设计**，
# 而不是为了把活干好。代价很实在：远程供应商报 200K，我们按 32K 跑，
# 能力被我们自己砍掉五分之四。
#
# 判据用这一句就够：**这个数字只会因为模型更强而被突破吗？** 是 → 它必须
# 可调，且默认不该压低。想限制就显式给 --window（那条路一直通着）。
#
# 代价要说清：配额是**比例**，窗口变大意味着每个请求最多能装的东西也变多
# （代码块 35% → 200K 窗口下最多 70K）。那是花钱的取舍，不是能力的取舍；
# 用量报表里看得见，由使用者定，而不是由我们替他砍。
DEFAULT_WINDOW = 8192


def resolve_window(gateway: ModelGateway, requested: int) -> int:
    """决定本次运行使用多大的上下文窗口。

    显式指定优先；否则问供应商；问不到才退回默认值。
    这个值不该由使用者猜：同一个配置文件下换模型或换 KV cache 量化，
    窗口都会变，只有服务端知道真实值。
    """
    if requested > 0:
        return requested
    detected = gateway.context_window()
    if not detected:
        return DEFAULT_WINDOW
    return detected


def resolve_scope(
    args: argparse.Namespace,
    step_scope: Sequence[str] = (),
    default: Sequence[str] = (),
) -> tuple[str, ...]:
    """决定本次运行允许自动落盘的范围。

    优先级：命令行的 --scope > 计划步骤声明的 scope > default。

    默认回退成空而不是全项目。「没声明范围」应该意味着「不许自动落盘」，
    会走逐项确认；把「没声明」当成「全都允许」是危险的默认值。

    更实际的原因是：策略是持久化的。一旦把 auto 存下来，之后每一次
    非计划运行都会变成**整个项目免确认**——那不是用户的持续选择，
    只是他某一次的选择被默默放大了。要走自动就显式给 --scope。
    """
    if args.scope:
        return tuple(part.strip() for part in args.scope.split(",") if part.strip())
    if step_scope:
        return tuple(step_scope)
    return tuple(default)


def resolve_policy(args: argparse.Namespace, project_root: Path) -> str:
    """命令行指定的策略优先，否则用已保存的。"""
    if args.policy:
        return args.policy
    return load_policy(policy_path(project_root))


def report_policy(policy: str, scope: Sequence[str]) -> None:
    print(f"授权策略：{policy} —— {describe_policy(policy)}")
    if policy == "auto":
        print(
            f"自动落盘范围：{'、'.join(scope)}"
            if scope
            else "自动落盘范围：（未指定 --scope，改动仍会逐项确认）"
        )
