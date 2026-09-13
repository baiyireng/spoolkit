from pathlib import Path

from spoolkit.agent.loop import SYSTEM_PROMPT, build_workflow
from spoolkit.agents.dispatcher import PLAN_PROMPT
from spoolkit.agents.plan import DECOMPOSE_PROMPT
from spoolkit.context import templates as T
from spoolkit.memory.distill import GROUP_TITLES, PROMPT
from spoolkit.tools.fs import read_file_spec
from spoolkit.tools.registry import ToolRegistry


def test_模板带版本号() -> None:
    assert T.PROMPT_VERSION


def test_各模块引用的是同一份模板() -> None:
    """内联字符串散在四处时，改一处要翻四个文件，还容易漏。"""
    assert SYSTEM_PROMPT is T.SYSTEM
    assert DECOMPOSE_PROMPT is T.DECOMPOSE
    assert PLAN_PROMPT is T.DISPATCH
    assert PROMPT is T.DISTILL
    assert GROUP_TITLES is T.DISTILL_GROUP_TITLES


def test_模板里没有忘记替换的占位符(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path))
    text = SYSTEM_PROMPT.format(tools="工具", workflow=build_workflow(registry))
    assert "{tools}" not in text
    assert "{workflow}" not in text


def test_工作方式片段都是非空的() -> None:
    for name in dir(T):
        if name.startswith("WORKFLOW_"):
            assert getattr(T, name).strip()


def test_分解模板要求声明改动范围() -> None:
    # 没有这条要求，步骤就不会带 scope，计划级授权会退化成逐项确认
    assert "scope" in T.DECOMPOSE


def test_归纳模板要求lesson带触发词() -> None:
    # 没有触发词的教训永远推不出去，等于白记
    assert "trigger" in T.DISTILL
    assert "必须" in T.DISTILL

