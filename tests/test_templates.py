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


def test_分解模板要求功能单元粒度() -> None:
    # dogfood 暴露：技术切片拆法导致一步崩全盘崩 + patch 死循环
    assert "功能单元" in T.DECOMPOSE
    assert "技术切片" in T.DECOMPOSE


def test_分解模板引导单文件整文件写入() -> None:
    # dogfood 暴露：分步改同一文件每步生成 patch 容易死循环
    assert "整文件" in T.DECOMPOSE or "write_file" in T.DECOMPOSE


def test_工作方式告诉agent不要自己提取JS验证() -> None:
    # dogfood 暴露：agent 自己提取 JS 做 node --check，试跑副本里没有文件，死循环
    assert "node --check" in T.WORKFLOW_RUN
    assert "自动跑验证" in T.WORKFLOW_RUN or "自动验" in T.WORKFLOW_RUN


def test_工作方式给出按产出类型的验证对照表() -> None:
    """写 HTML 时该用什么验，提示词里原先一个字都没有——模型只能去撞命令。

    所以这张表是「一条一行」的对照：产出类型 → 用哪种验证。
    """
    for row in (".html", ".js", ".py", ".json", ".toml", ".css"):
        assert row in T.WORKFLOW_VERIFY
    assert "verify_file" in T.WORKFLOW_VERIFY
    # 有牙：要写明「查不了」会说出来，不然模型会以为没报错就是全验过了
    assert "查不了" in T.WORKFLOW_VERIFY


def test_没注册验证工具时对照表不出现(tmp_path: Path) -> None:
    """提到一个没注册的工具，就是给模型一条它做不到的指令。"""
    from spoolkit.tools.exec import run_command_spec
    from spoolkit.tools.verify import verify_file_spec

    bare = ToolRegistry()
    bare.register(run_command_spec(tmp_path))
    assert "verify_file" not in build_workflow(bare)

    full = ToolRegistry()
    full.register(run_command_spec(tmp_path))
    full.register(verify_file_spec(tmp_path))
    assert "verify_file" in build_workflow(full)


def test_归纳模板要求lesson带触发词() -> None:
    # 没有触发词的教训永远推不出去，等于白记
    assert "trigger" in T.DISTILL
    assert "必须" in T.DISTILL


def test_督导模板要求读短环() -> None:
    assert "短环" in T.SUPERVISE or "振荡" in T.SUPERVISE

