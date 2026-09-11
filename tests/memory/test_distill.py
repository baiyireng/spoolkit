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


class _TruncatingModel:
    """前两次调用都截断，第三次才正常返回，用于验证降级路径。"""

    def __init__(self) -> None:
        self.requests: list = []

    def chat(self, request):
        from agents_dev.llm.types import ChatResponse

        self.requests.append(request)
        if len(self.requests) <= 2:
            return ChatResponse(
                text='{"entries":[{"kind":"fact","text":"被截断的内容"',
                prompt_tokens=1,
                completion_tokens=1,
                truncated=True,
            )
        return ChatResponse(
            text=json.dumps(
                {"entries": [{"kind": "fact", "text": "最重要的一条"}]},
                ensure_ascii=False,
            ),
            prompt_tokens=1,
            completion_tokens=1,
        )


def test_截断时先放大预算再降级条目数() -> None:
    model = _TruncatingModel()
    result = distill(model, _state(), limit=5)
    assert result == [("fact", "最重要的一条")]
    assert [r.max_tokens for r in model.requests[:3]] == [2048, 4096, 4096]
