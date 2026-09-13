"""`.agent/` 永远可写。

不加这一条会出现一个自相矛盾：提示词里让模型把临时脚本写进 `.agent/scratch/`
（那是我们自己在工作方式里写的），而范围闸门又把那处改动丢掉——实测就这么
丢过（一个真实需求里它想写个验证脚本，被挡之后只看到"验证失败"）。

这不是给模型扩权：`.agent/` 是 agent 自己的工作目录（计划、进度、索引、
scratch、基线），不是用户的代码。
"""

import argparse
from pathlib import Path

from spoolkit.agents.plan import PlanStep
from spoolkit.cli.commands.plan import _effective_scope


def test_范围里总是带上_agent() -> None:
    step = PlanStep(index=1, goal="改一处", acceptance="过", scope=("src/a.py",))
    assert _effective_scope(("**",), step) == ("src/a.py", ".agent/")


def test_已经包含_agent_时不重复() -> None:
    step = PlanStep(
        index=1, goal="改一处", acceptance="过", scope=(".agent/scratch/x.py",)
    )
    assert _effective_scope(("**",), step) == (".agent/scratch/x.py",)


def test_提议全被驳回时回退到授权范围并带上_agent() -> None:
    step = PlanStep(index=1, goal="改别处", acceptance="过", scope=("D:/elsewhere",))
    assert _effective_scope(("src/",), step) == ("src/", ".agent/")
