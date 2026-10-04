"""原地打转的检测。

小模型在短上下文里失去方向时会反复做同一个动作，而且不会自己停。
回归集里实测本地 Qwen2.5-Coder-7B 连续 10 次调用同一个 find_callers、
参数一字不差，把 12 步预算全烧光。这组测试盯的就是那种循环。
"""

import json
from pathlib import Path

from spoolkit.agent.loop import REPEAT_BLOCK_AT, AgentLoop, call_signature
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.edit import PendingChanges, write_file_spec
from spoolkit.tools.fs import list_dir_spec, read_file_spec
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.types import ToolCall


def _turn(thought: str, calls=None, final=None) -> str:
    return json.dumps(
        {
            "thought": thought,
            "tool_calls": calls or [],
            "state": None,
            "done": final is not None,
            "final": final,
        },
        ensure_ascii=False,
    )


def _build(tmp_path: Path, script: list) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path))
    registry.register(list_dir_spec(tmp_path))
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=OfflineTokenCounter()),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        # 这个文件测的是**机械层**：拦截发生在调用之前，判据是签名。
        # 督导会在打转升级时插一次调用，那是另一条路——它有自己的测试文件，
        # 插进来只会把脚本顺序搅乱。
        config=Config(
            project_root=tmp_path,
            context_window=4096,
            max_steps=10,
            supervise=False,
        ),
    )


def _last_prompt(loop: AgentLoop) -> str:
    """模型最后一次实际读到的内容。"""
    return "\n".join(m.content for m in loop.gateway.requests[-1].messages)


def _read(path: str = "a.txt") -> dict:
    return {"name": "read_file", "arguments": {"path": path}}


def test_参数顺序不同仍算同一次调用() -> None:
    """模型换个字段顺序就能绕过检测的话，这道防线等于没有。"""
    assert call_signature(ToolCall("t", {"x": 1, "y": 2})) == call_signature(
        ToolCall("t", {"y": 2, "x": 1})
    )
    assert call_signature(ToolCall("t", {"x": 1})) != call_signature(
        ToolCall("t", {"x": 2})
    )
    assert call_signature(ToolCall("t", {"x": 1})) != call_signature(
        ToolCall("u", {"x": 1})
    )


def test_第二次相同调用会提醒但仍然执行(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("内容\n", encoding="utf-8")
    loop = _build(
        tmp_path,
        [
            _turn("读", [_read()]),
            _turn("再读一次", [_read()]),
            _turn("看完了", final="里面有内容"),
        ],
    )
    executed: list[str] = []
    real = loop.registry.invoke

    def counting(call):
        executed.append(call.name)
        return real(call)

    loop.registry.invoke = counting
    loop.run("看看 a.txt")

    assert len(executed) == 2, "第二次应当照常执行"
    # 钉要点：提醒必须说实话——这里上一步就是同一个调用，所以它该说的是
    # 「与上一步完全相同」，而不是那句「最近几步里做过」。
    assert "这一步与上一步完全相同" in _last_prompt(loop)


def test_连续第三次不再执行(tmp_path: Path) -> None:
    """中间没有别的动作，结果不可能变——再跑一遍纯属烧预算。"""
    (tmp_path / "a.txt").write_text("内容\n", encoding="utf-8")
    loop = _build(
        tmp_path,
        [
            _turn("读", [_read()]),
            _turn("再读", [_read()]),
            _turn("还读", [_read()]),
            _turn("算了", final="结束"),
        ],
    )
    executed: list[str] = []
    real = loop.registry.invoke

    def counting(call):
        executed.append(call.name)
        return real(call)

    loop.registry.invoke = counting
    result = loop.run("看看 a.txt")

    assert REPEAT_BLOCK_AT == 3
    assert len(executed) == 2
    assert "没有执行" in _last_prompt(loop)
    assert any("重复调用第 3 次" in line for line in result.trace)


def test_中间插入别的调用后计数清零(tmp_path: Path) -> None:
    """读文件 → 看目录 → 再读同一个文件，是正常的，不该被拦。"""
    (tmp_path / "a.txt").write_text("内容\n", encoding="utf-8")
    loop = _build(
        tmp_path,
        [
            _turn("读", [_read()]),
            _turn("列目录", [{"name": "list_dir", "arguments": {"path": "."}}]),
            _turn("再读", [_read()]),
            _turn("完成", final="好"),
        ],
    )
    executed: list[str] = []
    real = loop.registry.invoke

    def counting(call):
        executed.append(call.name)
        return real(call)

    loop.registry.invoke = counting
    loop.run("看看 a.txt")

    assert len(executed) == 3
    # 中间那一步是别的调用，所以不许出现「与上一步完全相同」这个说法。
    assert "这一步与上一步完全相同" not in _last_prompt(loop)


def test_交替打转也会被拦住(tmp_path: Path) -> None:
    """A、B、A、B… 每一步都和上一步不同，只看「连续相同」会全漏掉。

    实测审查者用 git status / git diff 交替复读了 11 步，一次都没被拦。
    """
    (tmp_path / "a.txt").write_text("内容\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("内容\n", encoding="utf-8")
    pair = [
        _turn("看 a", [_read("a.txt")]),
        _turn("看 b", [_read("b.txt")]),
    ]
    loop = _build(tmp_path, pair * 4 + [_turn("算了", final="结束")])
    executed: list[str] = []
    real = loop.registry.invoke

    def counting(call):
        executed.append(call.arguments.get("path", ""))
        return real(call)

    loop.registry.invoke = counting
    result = loop.run("来回看")

    # 12 轮里每个文件最多真正读 2 次，第三次起被拦
    assert executed.count("a.txt") <= 2
    assert any("重复调用" in line for line in result.trace)


def test_阈值可以按工作区覆盖(tmp_path: Path) -> None:
    """默认阈值是按本机小模型实测的，不是普适真理。

    强模型上「连续两次同一调用」很可能是合理的（它一次读不完就再看一遍），
    所以这些数必须能改——而且改完要真的生效，不能只是打印出来好看。

    文件必须真的存在：这条测的是**重复**阈值，而读一个不存在的文件是失败，
    失败过的调用走的是另一条闸（`failed_repeat_block_at`，第一次重提即拦）。
    """
    (tmp_path / "a.txt").write_text("内容\n", encoding="utf-8")
    calls = [{"name": "read_file", "arguments": {"path": "a.txt"}}]
    loop = _build(tmp_path, [_turn("看", calls), _turn("再看", calls), _turn("好了", final="好")])
    loop.config = Config(
        project_root=tmp_path,
        context_window=4096,
        max_steps=6,
        supervise=False,
        overrides={"repeat_block_at": 10, "repeat_warn_at": 9},
    )
    executed: list[str] = []
    real = loop.registry.invoke

    def counting(call):
        executed.append(call.arguments.get("path", ""))
        return real(call)

    loop.registry.invoke = counting
    loop.run("看同一个文件两遍")
    # 阈值放宽到 10：两次相同的调用都该真的执行
    assert executed.count("a.txt") == 2


# ---- 「三道墙」之二：失败过的调用原样重提必须硬拦 ---------------------------

_TWO_DOCS = "\n".join(
    [
        "<html>",
        "<style>",
        ".snake { color: red; }",
        "#board { margin: 0; }",
        "body { background: black; }",
        "</style>",
        "<body>",
        '<div id="board" onclick="draw()"></div>',
        "<script>",
        "const canvas = 1;",
        "function draw() { return canvas; }",
        "let score = 0;",
        "function restart() { score = 0; }",
        "canvas.addEventListener('keydown', restart);",
        "</script>",
        "</body>",
    ]
    * 3
) + "\n"

_MERGED = "<html>\n<script>\nconst canvas = 1;\n</script>\n</html>\n"


def _edit_loop(tmp_path: Path, script: list, **kwargs) -> AgentLoop:
    pending = PendingChanges(tmp_path)
    registry = ToolRegistry()
    registry.register(write_file_spec(tmp_path, pending))
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=OfflineTokenCounter()),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(
            project_root=tmp_path,
            context_window=4096,
            max_steps=12,
            supervise=False,
            **kwargs,
        ),
    )


def _write(content: str, **extra) -> dict:
    return {
        "name": "write_file",
        "arguments": {"path": "snake.html", "content": content, **extra},
    }


def test_同一个失败调用原样重提被硬拦(tmp_path: Path) -> None:
    """dogfood 现场：write_file 被护栏拒绝后，模型一字不改地又提了一次。

    中间夹了一次 read_file，所以「连续相同」的计数被清零——旧行为于是放它
    又跑了一遍，白吃一步。失败过的参数重提不可能有别的结果，直接硬拦。
    """
    (tmp_path / "snake.html").write_text(_TWO_DOCS, encoding="utf-8")
    loop = _edit_loop(
        tmp_path,
        [
            _turn("整份重写", [_write(_MERGED)]),  # 被护栏拒绝
            _turn("再看一眼", [_read("snake.html")]),  # 中间插一次读（计数清零）
            _turn("原样再来", [_write(_MERGED)]),  # 同一个调用 → 硬拦
            _turn("算了", final="结束"),
        ],
    )
    executed: list[str] = []
    real = loop.registry.invoke

    def counting(call):
        if call.name == "write_file":
            executed.append(call.name)
        return real(call)

    loop.registry.invoke = counting
    result = loop.run("合并成一份单文档")

    assert executed == ["write_file"], "失败过的 write_file 不该跑第二遍"
    prompt = _last_prompt(loop)
    assert "完全相同" in prompt
    assert "没有执行" in prompt
    # 硬拦必须带可执行的替代方案：这里就是「先删重复段」的那条具体调用。
    assert "照做的下一步" in prompt
    assert "replace_lines(" in prompt
    assert "start_line=" in prompt


def test_硬拦的编辑动作不占步数预算(tmp_path: Path) -> None:
    """被拦的动作没有产出，不该白吃预算——但它也不能让预算变成无限。"""
    (tmp_path / "snake.html").write_text(_TWO_DOCS, encoding="utf-8")
    loop = _edit_loop(
        tmp_path,
        [
            _turn("整份重写", [_write(_MERGED)]),
            _turn("原样再来", [_write(_MERGED)]),
            _turn("还来", [_write(_MERGED)]),
            _turn("再来", [_write(_MERGED)]),
            _turn("继续来", [_write(_MERGED)]),
            _turn("算了", final="结束"),
        ],
    )
    result = loop.run("合并成一份单文档")

    refunds = [line for line in result.trace if "不占步数预算" in line]
    assert refunds, "硬拦没有退还步数预算"
    assert any("已退还 1/" in line for line in refunds)
    # 退还有上限：默认 3 步之后照旧占预算（否则「不断被拦」就是无限预算）。
    assert len(refunds) == 3, f"退还步数超出上限：{refunds}"


def test_失败重提的拦截阈值可以按工作区覆盖(tmp_path: Path) -> None:
    """`failed_repeat_block_at` 是可覆盖的：调到 3 就允许它再试一次。"""
    (tmp_path / "snake.html").write_text(_TWO_DOCS, encoding="utf-8")
    loop = _edit_loop(
        tmp_path,
        [
            _turn("整份重写", [_write(_MERGED)]),
            _turn("再来", [_write(_MERGED)]),
            _turn("第三次", [_write(_MERGED)]),
            _turn("算了", final="结束"),
        ],
        overrides={"failed_repeat_block_at": 3},
    )
    executed: list[str] = []
    real = loop.registry.invoke

    def counting(call):
        if call.name == "write_file":
            executed.append(call.name)
        return real(call)

    loop.registry.invoke = counting
    loop.run("合并成一份单文档")
    # 阈值 3 = 允许一次重试；第二次重提才拦。
    assert len(executed) == 2


def test_步数预算随目标文件规模放大(tmp_path: Path) -> None:
    """dogfood：303 行的合并用满 10 步仍失败。规模在第一步就看得见，预算该跟着走。

    基线 10 步 + 300 行目标按 60 行/步补 5 步 = 15 步；脚本给 11 步编辑 +
    收尾，旧行为会在第 10 步撞墙，新行为跑得完。
    """
    (tmp_path / "big.py").write_text(
        "\n".join(f"line{i}" for i in range(300)) + "\n", encoding="utf-8"
    )
    pending = PendingChanges(tmp_path)
    registry = ToolRegistry()
    registry.register(write_file_spec(tmp_path, pending))
    script = [
        _turn(
            f"改第 {i} 处",
            [
                {
                    "name": "write_file",
                    "arguments": {
                        "path": "big.py",
                        # 每次都是不同的正文（不同指纹），但都是 300 行左右，
                        # 所以每次都够得着「按规模放大」这条。
                        "content": "\n".join(f"LINE{i}-{j}" for j in range(300)) + "\n",
                    },
                }
            ],
        )
        for i in range(11)
    ]
    script.append(_turn("完成", final="改完了"))
    loop = AgentLoop(
        gateway=FakeModel(script=script, tokenizer=OfflineTokenCounter()),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(
            project_root=tmp_path,
            context_window=4096,
            max_steps=10,
            supervise=False,
        ),
    )
    result = loop.run("改一份 300 行的文件")

    assert any("步数预算按规模放大到 15" in line for line in result.trace), result.trace
    assert result.finished is True
    # 11 步编辑 + 收尾那一步 = 12；旧行为在第 10 步就撞墙了（收不了尾）。
    assert result.steps == 12, "放大后的预算没让它多走那几步"


def test_参数校验失败的原样重提把修正后的形状端回去(tmp_path: Path) -> None:
    """现场：`search_code` 带着不支持的参数 `source` 连发 3 次，被拦 2 次后放弃。

    第一次是**参数校验**拦的（`tools/registry.py` 的拒绝话术，里面带一行
    「照做的下一步：… search_code(pattern="pricing")」）；它一字不改地又发一次时，
    循环的硬拦认的正是那个前缀，于是把**同一个形状**端回去。两处给的是同一句
    话——模型不会从两个地方拿到两种说法。
    """
    from spoolkit.tools.types import NEXT_STEP_MARKER, ToolResult, ToolSpec

    registry = ToolRegistry()
    calls: list[dict] = []

    def handler(args):
        calls.append(args)
        return ToolResult(ok=True, content="")

    registry.register(
        ToolSpec(
            name="search_code",
            description="按正则在代码里搜",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "搜索用的正则"},
                    "path": {"type": "string", "description": "在哪个子目录里找"},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            handler=handler,
        )
    )
    bad = {"name": "search_code", "arguments": {"pattern": "pricing", "source": "p.html"}}
    loop = AgentLoop(
        gateway=FakeModel(
            script=[
                _turn("搜一下导航在哪", [bad]),
                _turn("原样再来", [bad]),
                _turn("算了", final="结束"),
            ],
            tokenizer=OfflineTokenCounter(),
        ),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(
            project_root=tmp_path, context_window=4096, max_steps=6, supervise=False
        ),
    )

    result = loop.run("把 pricing 的导航并进 index")

    assert calls == [], "带不支持参数的调用一次都不该真的跑起来"
    prompt = _last_prompt(loop)
    assert "完全相同" in prompt and "没有执行" in prompt
    assert NEXT_STEP_MARKER in prompt
    # 端回去的是那个**能照抄**的形状：工具名 + 留下的合法值，没有 source。
    shape = [
        line for line in prompt.splitlines() if NEXT_STEP_MARKER in line
    ]
    assert any('search_code(pattern="pricing")' in line for line in shape), prompt
    assert all("source=" not in line for line in shape)
    assert result.finished is True
