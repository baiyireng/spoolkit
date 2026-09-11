import json
from pathlib import Path

from agents_dev.agents.runtime import IMPLEMENTER, TaskSpec, run_role
from agents_dev.config import Config
from agents_dev.llm.fake import FakeModel
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.tools.registry import ToolRegistry


def _turn(final: str) -> str:
    return json.dumps(
        {"thought": "结束", "tool_calls": [], "state": None, "done": True, "final": final},
        ensure_ascii=False,
    )


def test_子智能体预算大于主循环预算() -> None:
    config = Config(project_root=Path("."))
    assert config.subagent_steps > config.max_steps


def test_默认值符合预期() -> None:
    config = Config(project_root=Path("."))
    assert config.max_steps == 10
    assert config.subagent_steps == 20


def test_两个预算都可以显式覆盖() -> None:
    config = Config(project_root=Path("."), max_steps=3, subagent_steps=7)
    assert (config.max_steps, config.subagent_steps) == (3, 7)


def test_子智能体按配置的预算运行(tmp_path: Path) -> None:
    tokenizer = OfflineTokenCounter()
    gateway = FakeModel(script=[_turn("完成")], tokenizer=tokenizer)
    result = run_role(
        IMPLEMENTER,
        TaskSpec(goal="做点事", acceptance="完成"),
        gateway,
        tokenizer,
        ToolRegistry(),
        Config(project_root=tmp_path, context_window=4096, subagent_steps=5),
    )
    assert result.finished is True


def test_显式传入步数时覆盖配置(tmp_path: Path) -> None:
    tokenizer = OfflineTokenCounter()
    # 脚本只够一步，显式限制为 1 步应当能正常结束
    gateway = FakeModel(script=[_turn("完成")], tokenizer=tokenizer)
    result = run_role(
        IMPLEMENTER,
        TaskSpec(goal="做点事", acceptance="完成"),
        gateway,
        tokenizer,
        ToolRegistry(),
        Config(project_root=tmp_path, context_window=4096, subagent_steps=9),
        max_steps=1,
    )
    assert result.finished is True


def test_子智能体撞上限时如实报告未完成(tmp_path: Path) -> None:
    tokenizer = OfflineTokenCounter()
    gateway = FakeModel(script=["不是 JSON"] * 5, tokenizer=tokenizer)
    result = run_role(
        IMPLEMENTER,
        TaskSpec(goal="做点事", acceptance="完成"),
        gateway,
        tokenizer,
        ToolRegistry(),
        Config(project_root=tmp_path, context_window=4096, subagent_steps=3),
    )
    assert result.finished is False
    assert result.steps == 3

