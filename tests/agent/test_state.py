from pathlib import Path

from spoolkit.agent.state import StateDelta, TaskState, load_state, save_state
from spoolkit.llm.tokenizer import OfflineTokenCounter


def _state() -> TaskState:
    return TaskState(
        task_id="t1",
        goal="给 parser 加增量更新",
        done=["读 parser.py"],
        current="正在改 _parse_file",
        verify="跑 pytest tests/test_parser.py",
        excluded=["整文件重解析"],
        hypothesis="比较 mtime",
        step=1,
    )


def test_渲染包含全部字段() -> None:
    text = _state().render()
    for fragment in (
        "给 parser 加增量更新",
        "读 parser.py",
        "_parse_file",
        "pytest",
        "整文件重解析",
        "mtime",
    ):
        assert fragment in text


def test_渲染控制在很小体积() -> None:
    assert _state().token_cost(OfflineTokenCounter()) < 120


def test_应用增量更新字段() -> None:
    state = _state()
    state.apply(StateDelta(done_added=["确认索引表结构"], current="写测试"))
    assert "确认索引表结构" in state.done
    assert state.current == "写测试"


def test_未提供的字段保持不变() -> None:
    state = _state()
    state.apply(StateDelta(hypothesis="改用哈希比对"))
    assert state.current == "正在改 _parse_file"
    assert state.hypothesis == "改用哈希比对"


def test_保存后可原样读回(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_state(_state(), path)
    restored = load_state(path)
    assert restored is not None
    assert restored.render() == _state().render()


def test_读取不存在的检查点返回空(tmp_path: Path) -> None:
    assert load_state(tmp_path / "nope.json") is None


def test_步数可递增() -> None:
    state = _state()
    state.step_forward()
    assert state.step == 2

