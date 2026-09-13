"""拆解之前必须先看一眼环境。

实测抓到的形状：`decompose` 不看环境时产出的计划**形状完美、内容全错**
——20 步、每步都有验收标准和范围，而范围写的是 `task_1/`…`task_20/`
（真实目录是 `01_off_by_one`…）。那些目录根本不存在，于是每一步的范围
闸门都会把改动挡在外面，整份计划等于没有。

还有一条同类缺陷：`--autonomous` 曾经一执行就崩（`Config` 没导入），
也就是说这条路从来没被真正跑通。所以这里补一个冒烟测试。
"""

import argparse
import json
from pathlib import Path

from spoolkit.cli.commands.plan import _survey, autonomous
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter


def _plan(goal: str) -> str:
    return json.dumps(
        {
            "steps": [
                {
                    "goal": "改 01_off_by_one/calc.py",
                    "acceptance": "在 01_off_by_one/ 下跑 pytest 通过",
                    "scope": ["01_off_by_one/"],
                    "executor": "self",
                    "note": "",
                }
            ]
        },
        ensure_ascii=False,
    )


def _turn(final: str) -> str:
    return json.dumps(
        {"thought": "收尾", "tool_calls": [], "state": None, "done": True, "final": final},
        ensure_ascii=False,
    )


def _workspace(tmp_path: Path) -> Path:
    (tmp_path / "01_off_by_one").mkdir()
    (tmp_path / "01_off_by_one" / "calc.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "02_empty_input").mkdir()
    return tmp_path


def test_拆解前先看工作区(tmp_path: Path) -> None:
    """收集到的东西里必须有**真实目录名**——计划的范围就靠它对齐。"""
    root = _workspace(tmp_path)
    survey = _survey(root, "把题做完", OfflineTokenCounter())
    assert "01_off_by_one" in survey
    assert "02_empty_input" in survey
    assert ".agent" not in survey


def test_自主模式把环境信息喂给拆解(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    gateway = FakeModel(
        # 第二份是覆盖检查要的补排：假计划只覆盖了 01，工作区里还有 02。
        script=[
            _plan("把题做完"),
            json.dumps(
                {
                    "steps": [
                        {
                            "goal": "改 02_empty_input/stats.py",
                            "acceptance": "在 02_empty_input/ 下跑 pytest 通过",
                            "scope": ["02_empty_input/"],
                            "executor": "self",
                        }
                    ],
                    "done": True,
                },
                ensure_ascii=False,
            ),
            _turn("做完了"),
            _turn("做完了"),
        ],
        tokenizer=OfflineTokenCounter(),
    )
    args = argparse.Namespace(
        goal="把题做完",
        scope="**",
        limit=1,
        policy="auto",
        window=4096,
        max_steps=2,
        subagent_steps=2,
        no_memory=True,
        session="cli",
        provider="fake",
        model="",
        base_url="",
        proxy="",
    )
    assert autonomous(args, root, gateway) == 0
    prompt = gateway.requests[0].messages[0].content
    # 拆解那一次调用里带着真实目录名（不看环境的版本这里是「无」）
    assert "01_off_by_one" in prompt


def test_拆解要问清这一步谁来做() -> None:
    """「按步注入」的每一步都是一份派发契约（goal + 验收 + 范围）。

    不写「谁来做」这一维，harness 就等于替所有步骤决定「都自己做」——
    派发的决定权与**独立审查**就都丢了。实测那条路里 `dispatch` 用了 0 次。
    """
    from spoolkit.agents.plan import SELF, SUBAGENT, parse_plan
    from spoolkit.context import templates as T

    assert "executor" in T.DECOMPOSE
    plan = parse_plan(
        json.dumps(
            {
                "steps": [
                    {
                        "goal": "A",
                        "acceptance": "a",
                        "scope": ["x/"],
                        "executor": "subagent",
                    },
                    {"goal": "B", "acceptance": "b", "scope": ["y/"]},
                ]
            },
            ensure_ascii=False,
        ),
        "目标",
    )
    assert plan.steps[0].executor == SUBAGENT
    # 没写的按「自己做」——派发要付固定成本，不该由缺省打开
    assert plan.steps[1].executor == SELF
