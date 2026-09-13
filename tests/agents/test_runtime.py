import json
from pathlib import Path

import pytest

from spoolkit.agents.runtime import (
    IMPLEMENTER,
    REVIEWER,
    ROLES,
    TaskSpec,
    restrict,
    run_role,
)
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.edit import PendingChanges, replace_lines_spec, write_file_spec
from spoolkit.tools.fs import read_file_spec
from spoolkit.tools.registry import ToolRegistry


def _turn(final: str) -> str:
    return json.dumps(
        {"thought": "结束", "tool_calls": [], "state": None, "done": True, "final": final},
        ensure_ascii=False,
    )


def _registry(tmp_path: Path, pending: PendingChanges) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path))
    registry.register(write_file_spec(tmp_path, pending))
    registry.register(replace_lines_spec(tmp_path, pending))
    return registry


def _run(tmp_path: Path, role, spec, final: str = "完成"):
    tokenizer = OfflineTokenCounter()
    pending = PendingChanges(tmp_path)
    loop = None
    gateway = FakeModel(script=[_turn(final)], tokenizer=tokenizer)
    result = run_role(
        role,
        spec,
        gateway,
        tokenizer,
        _registry(tmp_path, pending),
        Config(project_root=tmp_path, context_window=4096),
    )
    return result, gateway


def test_缺少验收标准不允许派发() -> None:
    problem = TaskSpec(goal="改点东西").validate()
    assert problem is not None
    assert "验收标准" in problem


def test_目标为空不允许派发() -> None:
    assert TaskSpec(goal="  ", acceptance="能跑通").validate() is not None


def test_要素齐全时通过校验() -> None:
    spec = TaskSpec(goal="修复解析", acceptance="pytest 全绿")
    assert spec.validate() is None


def test_渲染出的说明包含全部要素() -> None:
    spec = TaskSpec(
        goal="修复解析",
        targets=("parser.py", "parse_config"),
        constraints=("函数不超过 40 行",),
        acceptance="pytest 全绿",
        out_of_scope=("不要动 render.py",),
    )
    text = spec.render()
    for fragment in ("修复解析", "parser.py", "40 行", "pytest 全绿", "render.py"):
        assert fragment in text


def test_审查者没有写权限() -> None:
    assert REVIEWER.can_write is False
    assert "write_file" not in REVIEWER.tools
    assert "replace_lines" not in REVIEWER.tools


def test_实现者有写权限() -> None:
    assert IMPLEMENTER.can_write is True
    assert "replace_lines" in IMPLEMENTER.tools


def test_角色登记表包含两个角色() -> None:
    assert set(ROLES) == {"implementer", "reviewer"}


def test_裁剪工具集只保留角色允许的(tmp_path: Path) -> None:
    limited = restrict(
        _registry(tmp_path, PendingChanges(tmp_path)), REVIEWER
    )
    assert limited.names() == ("read_file",)


def test_裁剪后审查者调用写工具会被拒绝(tmp_path: Path) -> None:
    from spoolkit.tools.types import ToolCall

    limited = restrict(
        _registry(tmp_path, PendingChanges(tmp_path)), REVIEWER
    )
    result = limited.invoke(
        ToolCall(name="write_file", arguments={"path": "a.py", "content": "x"})
    )
    assert result.ok is False
    assert "未知工具" in result.content


def test_在独立上下文里跑通一个角色(tmp_path: Path) -> None:
    spec = TaskSpec(goal="写一个文件", acceptance="文件存在")
    result, _ = _run(tmp_path, IMPLEMENTER, spec, final="已完成")
    assert result.finished is True
    assert result.final == "已完成"


def test_子智能体不接预取与记忆(tmp_path: Path) -> None:
    spec = TaskSpec(goal="做点事", acceptance="完成")
    _, gateway = _run(tmp_path, IMPLEMENTER, spec)
    system = gateway.requests[0].messages[0].content
    assert "实现一处具体改动" in system
    assert "项目事实" not in system


def test_审查者收到的是独立提示词(tmp_path: Path) -> None:
    spec = TaskSpec(goal="审查改动", acceptance="给出结论")
    _, gateway = _run(tmp_path, REVIEWER, spec)
    system = gateway.requests[0].messages[0].content
    assert "独立审查" in system


def test_派发说明作为用户消息传入(tmp_path: Path) -> None:
    spec = TaskSpec(goal="修复解析", acceptance="pytest 全绿")
    _, gateway = _run(tmp_path, IMPLEMENTER, spec)
    user = [m for m in gateway.requests[0].messages if m.role == "user"]
    assert "pytest 全绿" in user[0].content


def test_校验失败时运行直接报错(tmp_path: Path) -> None:
    tokenizer = OfflineTokenCounter()
    with pytest.raises(ValueError):
        run_role(
            IMPLEMENTER,
            TaskSpec(goal="没写验收标准"),
            FakeModel(script=[_turn("x")], tokenizer=tokenizer),
            tokenizer,
            _registry(tmp_path, PendingChanges(tmp_path)),
            Config(project_root=tmp_path, context_window=4096),
        )

