"""会话记忆块只注入摘要和引用。空块不进入提示。"""

from spoolkit.agent.context_inject import ContextBlock, build_context_block, inject_context_block
from spoolkit.agent.context_pack import ContextPack, ContextSection

BODY = "只有正文里才有的句子甲。"
SECRET = "sk-test-secret-value"
GOAL = "写一份说明"


class LenCounter:
    def count(self, text: str) -> int:
        return len(text)


def _section(kind: str, lines: tuple[str, ...]) -> ContextSection:
    titles = {
        "task": "当前任务",
        "open": "未完成",
        "decision": "决定",
        "artifact": "产物",
        "done": "已完成",
        "meta": "最近一轮",
    }
    return ContextSection(kind=kind, title=titles[kind], lines=lines, tokens=1)  # type: ignore[arg-type]


def _pack(*sections: ContextSection, goal: str = GOAL, omitted: tuple[str, ...] = ()) -> ContextPack:
    return ContextPack(goal=goal, sections=sections, omitted=omitted, total_tokens=len(sections))


def test_empty_pack_stays_empty_and_is_not_injected() -> None:
    counter = LenCounter()
    block = build_context_block(
        ContextPack(goal="", sections=(), omitted=(), total_tokens=0),
        counter,
        1000,
    )
    assert block == ContextBlock(text="", tokens=0, sections=(), omitted=())
    prompt = "请写估算。"
    assert inject_context_block(prompt, block) is prompt
    goal_only = build_context_block(_pack(goal=GOAL), counter, 1000)
    assert goal_only.text == ""
    assert inject_context_block(prompt, goal_only) == prompt


def test_block_renders_sections_without_goal_or_body() -> None:
    counter = LenCounter()
    pack = _pack(
        _section("task", ("正在写估算",)),
        _section("open", ("还没写结尾",)),
        _section("decision", ("按三节展开",)),
        _section(
            "artifact",
            (
                "title: 估算说明",
                "summary: 三节估算。",
                "path: .agent/artifacts/sess-a/abc",
                "id: doc-1",
                BODY,
            ),
        ),
        _section("done", ("写完大纲",)),
        omitted=("meta",),
    )
    first = build_context_block(pack, counter, 100_000)
    second = build_context_block(pack, counter, 100_000)
    assert first == second
    assert first.text == second.text
    assert first.sections == ("task", "open", "decision", "artifact", "done")
    assert first.omitted == ("meta",)
    assert first.tokens == counter.count(first.text)
    assert first.text.startswith("【会话记忆】")
    for label in ("当前任务", "未完成", "决定", "产物", "已完成"):
        assert label in first.text
    assert "title: 估算说明" in first.text
    assert "summary: 三节估算。" in first.text
    assert "path: .agent/artifacts/sess-a/abc" in first.text
    assert "id: doc-1" in first.text
    assert BODY not in first.text
    assert GOAL not in first.text
    assert "略过 1 项" in first.text
    assert "meta" not in first.text
    injected = inject_context_block("请继续。", first)
    assert injected.startswith(first.text)
    assert injected.endswith("请继续。")


def test_budget_drops_whole_sections_and_counts_them() -> None:
    counter = LenCounter()
    long_line = "未" * 80
    pack = _pack(
        _section("task", ("正在写估算",)),
        _section("open", (long_line,)),
        _section("decision", ("按三节展开",)),
    )
    full = build_context_block(pack, counter, 100_000)
    assert long_line in full.text
    budget = full.tokens
    shrunk = full
    while budget > 0:
        budget -= 1
        shrunk = build_context_block(pack, counter, budget)
        if long_line not in shrunk.text and "正在写估算" in shrunk.text:
            break
    else:
        raise AssertionError("预算没有裁掉长节")
    assert shrunk.tokens <= budget
    assert shrunk.tokens == counter.count(shrunk.text)
    assert "正在写估算" in shrunk.text
    assert "按三节展开" not in shrunk.text or "open" in shrunk.omitted
    assert long_line not in shrunk.text
    assert "未完成" not in shrunk.text or shrunk.sections.count("open") == 0
    assert shrunk.omitted
    assert f"略过 {len(shrunk.omitted)} 项" in shrunk.text
    assert "open" not in shrunk.text


def test_goal_line_and_secret_are_not_repeated() -> None:
    counter = LenCounter()
    pack = _pack(
        _section("task", (GOAL, f"配置 API_KEY={SECRET} 之后继续")),
        _section("done", ("写完大纲",)),
    )
    block = build_context_block(pack, counter, 100_000)
    assert GOAL not in block.text
    assert SECRET not in block.text
    assert "API_KEY=***" in block.text
    assert "写完大纲" in block.text
    again = build_context_block(pack, counter, 100_000)
    assert again == block
