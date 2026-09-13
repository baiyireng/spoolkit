import json

from spoolkit.agent.state import TaskState
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.llm.types import ChatResponse
from spoolkit.memory.distill import MAX_SPLIT_DEPTH, distill, segments_of


def _model(script: list[str]) -> FakeModel:
    return FakeModel(script=script, tokenizer=OfflineTokenCounter())


def _state() -> TaskState:
    return TaskState(
        task_id="t1",
        goal="给 parser 加增量更新",
        done=["读 parser.py"],
        excluded=["整文件重解析"],
    )


def _entries(*pairs) -> str:
    items = []
    for pair in pairs:
        kind, text = pair[0], pair[1]
        trigger = pair[2] if len(pair) > 2 else ""
        items.append({"kind": kind, "text": text, "trigger": trigger})
    return json.dumps({"entries": items}, ensure_ascii=False)


class _SegmentSensitive:
    """材料条目越多越容易放不下，用来验证「分治优先于缩减要求」。

    刻意不按提示长度判定：长度依赖提示模板的具体字数，改一个字测试就飘了；
    按材料条目数判定才稳定地表达「这一批太多」。
    """

    def __init__(self, max_segments: int) -> None:
        self.requests: list = []
        self.max_segments = max_segments

    def chat(self, request):
        self.requests.append(request)
        prompt = request.messages[0].content
        # 提示模板里只有材料使用 "- " 列表，因此可以直接统计。
        material = [ln for ln in prompt.splitlines() if ln.startswith("- ")]
        if len(material) > self.max_segments:
            return ChatResponse(
                text='{"entries":[',
                prompt_tokens=1,
                completion_tokens=1,
                truncated=True,
            )
        text = material[0][2:][:30] if material else "无"
        return ChatResponse(
            text=_entries(("fact", text)), prompt_tokens=1, completion_tokens=1
        )

    def close(self) -> None:
        pass


class _AlwaysTruncating:
    def __init__(self) -> None:
        self.requests: list = []

    def chat(self, request):
        self.requests.append(request)
        return ChatResponse(
            text='{"entries":[',
            prompt_tokens=1,
            completion_tokens=1,
            truncated=True,
        )

    def close(self) -> None:
        pass


def test_解析归纳出的条目() -> None:
    model = _model(
        [
            _entries(
                ("fact", "测试命令是 pytest -q"),
                ("lesson", "先上约束", "解析,正则"),
            )
        ]
    )
    result = distill(model, _state())
    assert result.entries == [
        ("fact", "测试命令是 pytest -q", ""),
        ("lesson", "先上约束", "解析,正则"),
    ]
    assert result.split is False
    assert result.truncated is False


def test_非法JSON退化为空列表() -> None:
    assert distill(_model(["不是 JSON"]), _state()).entries == []


def test_未知类型被丢弃() -> None:
    raw = json.dumps({"entries": [{"kind": "胡乱类型", "text": "内容"}]}, ensure_ascii=False)
    assert distill(_model([raw]), _state()).entries == []


def test_空文本条目被丢弃() -> None:
    assert distill(_model([_entries(("fact", "   "))]), _state()).entries == []


def test_条目数量受上限约束() -> None:
    raw = _entries(*[(("fact"), f"事实{i}") for i in range(10)])  # type: ignore[arg-type]
    assert len(distill(_model([raw]), _state(), limit=3).entries) == 3


def test_材料里包含排除过的方案() -> None:
    model = _model([_entries(("fact", "事实"))])
    distill(model, _state(), final="这是一段答复")
    prompt = model.requests[0].messages[0].content
    assert "整文件重解析" in prompt
    assert "给 parser 加增量更新" in prompt
    assert "这是一段答复" in prompt


def test_归纳请求带结构约束() -> None:
    model = _model([_entries(("fact", "事实"))])
    distill(model, _state())
    assert model.requests[0].response_schema is not None


def test_材料被切成片段() -> None:
    state = TaskState(task_id="t", goal="g", done=["甲", "乙"], excluded=["丙"])
    segments = segments_of(state, "第一段\n\n第二段")
    assert segments == [
        ("过程", "甲"),
        ("过程", "乙"),
        ("已排除", "丙"),
        ("结论", "第一段"),
        ("结论", "第二段"),
    ]


def test_答复按段落切而不是按行切() -> None:
    state = TaskState(task_id="t", goal="g")
    segments = segments_of(state, "第一行\n第二行\n第三行")
    assert segments == [("结论", "第一行\n第二行\n第三行")]


def test_没有材料时也有兜底片段() -> None:
    assert segments_of(TaskState(task_id="t", goal="g"), "") == [
        ("材料", "（无额外内容）")
    ]


def test_截断时优先分治而不是缩减要求() -> None:
    # 整批 6 段过程放不下，半批 3 段放得下（提示里只有过程用 "- " 列表）。
    model = _SegmentSensitive(max_segments=4)
    state = TaskState(
        task_id="t",
        goal="目标",
        done=[f"步骤{i}" for i in range(6)],
    )
    result = distill(model, state, final="这是交给用户的答复。")
    assert result.truncated is True
    assert result.split is True
    assert len(result.entries) >= 2


def test_分治结果会去重() -> None:
    from spoolkit.memory.distill import _merge

    merged = _merge(
        [
            [("fact", "同一条", ""), ("fact", "左", "")],
            [("fact", "同一条", ""), ("fact", "右", "")],
        ],
        10,
    )
    assert merged == [
        ("fact", "同一条", ""),
        ("fact", "左", ""),
        ("fact", "右", ""),
    ]


def test_合并结果受上限约束() -> None:
    from spoolkit.memory.distill import _merge

    merged = _merge([[("fact", f"第{i}条", "") for i in range(5)]], 2)
    assert len(merged) == 2


def test_无法切分时才降级到一条() -> None:
    model = _AlwaysTruncating()
    state = TaskState(task_id="t", goal="目标")
    result = distill(model, state)
    assert result.entries == []
    assert result.split is False


def test_切分深度有上限() -> None:
    assert MAX_SPLIT_DEPTH == 2
    model = _AlwaysTruncating()
    state = TaskState(task_id="t", goal="目标", done=[f"步骤{i}" for i in range(8)])
    distill(model, state)
    # 深度 2 时最多 4 个叶节点，加上重试不应失控。
    assert len(model.requests) <= 16
