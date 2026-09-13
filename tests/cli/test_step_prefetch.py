"""执行一步时，预取必须按计划声明的范围走。

实测形状（同一批 5 题，第 4 步修一个 import）：步骤提示词里「已完成」
列着前几步的目标、「验收标准」写着 pytest、契约里写着 `不得改动 test_xxx.py`
——关键词排序被这些噪音带走，两个正文名额全给了**测试文件**，
真正要改的 `main.py` 和它旁边的 `helper.py` 一个都没进来。
结果是这一步花了 6 轮 12802 token，占整批的 37%。

修法不是「让它多读」，是**把范围当锚**：计划里已经写了这一步动哪个文件，
那是比关键词更准的信号。
"""

import argparse
import json
from pathlib import Path

from spoolkit.cli.commands.plan import autonomous
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter


def _plan() -> str:
    """计划里的措辞刻意**不含**文件名与函数名——预取该靠范围，不靠措辞。"""
    return json.dumps(
        {
            "steps": [
                {
                    "goal": "把这一题改好",
                    "acceptance": "改完能通过",
                    "scope": ["01_off_by_one/calc.py"],
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


def _args() -> argparse.Namespace:
    return argparse.Namespace(
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


def test_步骤预取列出同目录的条目(tmp_path: Path) -> None:
    """修 import 靠的就是「旁边还有个 helper.py」这条信息。

    断言挑的是 `notes.txt`：它没有符号、名字也不在提示词里，
    符号表和关键词排序**都不可能**带出它——只有列目录能。
    """
    (tmp_path / "01_off_by_one").mkdir()
    (tmp_path / "01_off_by_one" / "calc.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "01_off_by_one" / "helpers.py").write_text(
        "def double(x):\n    return x * 2\n", encoding="utf-8"
    )
    (tmp_path / "01_off_by_one" / "notes.txt").write_text("随手记\n", encoding="utf-8")
    gateway = FakeModel(
        script=[_plan(), _turn("改好了")], tokenizer=OfflineTokenCounter()
    )

    assert autonomous(_args(), tmp_path, gateway) == 0

    assert any(
        "notes.txt" in message.content for message in gateway.requests[1].messages
    )
