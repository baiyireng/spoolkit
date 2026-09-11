import json
from pathlib import Path

from agents_dev.agent.state import TaskState
from agents_dev.llm.fake import FakeModel
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.memory.distill import distill


def _model(script: list[str]) -> FakeModel:
    return FakeModel(script=script, tokenizer=OfflineTokenCounter())


def _state() -> TaskState:
    return TaskState(
        task_id="t1",
        goal="给 parser 加增量更新",
        done=["读 parser.py"],
        excluded=["整文件重解析"],
    )


def test_解析归纳出的条目() -> None:
    payload = json.dumps(
        {
            "entries": [
                {"kind": "fact", "text": "测试命令是 pytest -q"},
                {"kind": "lesson", "text": "先上语法约束再谈正则"},
            ]
        },
        ensure_ascii=False,
    )
    assert distill(_model([payload]), _state()) == [
        ("fact", "测试命令是 pytest -q"),
        ("lesson", "先上语法约束再谈正则"),
    ]


def test_非法JSON退化为空列表() -> None:
    assert distill(_model(["不是 JSON"]), _state()) == []


def test_未知类型被丢弃() -> None:
    payload = json.dumps(
        {"entries": [{"kind": "胡乱类型", "text": "内容"}]}, ensure_ascii=False
    )
    assert distill(_model([payload]), _state()) == []


def test_空文本条目被丢弃() -> None:
    payload = json.dumps(
        {"entries": [{"kind": "fact", "text": "   "}]}, ensure_ascii=False
    )
    assert distill(_model([payload]), _state()) == []


def test_条目数量受上限约束() -> None:
    payload = json.dumps(
        {"entries": [{"kind": "fact", "text": f"事实{i}"} for i in range(10)]},
        ensure_ascii=False,
    )
    assert len(distill(_model([payload]), _state(), limit=3)) == 3


def test_提示里带上排除过的方案() -> None:
    model = _model([json.dumps({"entries": []}, ensure_ascii=False)])
    distill(model, _state(), final="测试命令是 pytest -q")
    prompt = model.requests[0].messages[0].content
    assert "整文件重解析" in prompt
    assert "给 parser 加增量更新" in prompt
    assert "测试命令是 pytest -q" in prompt


def test_归纳请求带结构约束() -> None:
    model = _model([json.dumps({"entries": []}, ensure_ascii=False)])
    distill(model, _state())
    assert model.requests[0].response_schema is not None
