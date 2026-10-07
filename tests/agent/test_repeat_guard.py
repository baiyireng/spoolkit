"""原地打转的检测。

小模型在短上下文里失去方向时会反复做同一个动作，而且不会自己停。
回归集里实测本地 Qwen2.5-Coder-7B 连续 10 次调用同一个 find_callers、
参数一字不差，把 12 步预算全烧光。这组测试盯的就是那种循环。
"""

import json
import sys
from pathlib import Path

from spoolkit.agent.loop import REPEAT_BLOCK_AT, AgentLoop, call_signature
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.calc import calc_spec
from spoolkit.tools.edit import (
    PendingChanges,
    confirm_rewrite_spec,
    replace_text_spec,
    write_file_spec,
)
from spoolkit.tools.exec import run_command_spec
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


def _build(tmp_path: Path, script: list, overrides: dict | None = None) -> AgentLoop:
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
            overrides=overrides or {},
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


def test_历史还装得下时不该重置上下文(tmp_path: Path) -> None:
    """P0 取证抓出来的那条早重置：**工具输出必须活到下一次请求里**。

    旧口径 `hard_limit = window × 0.90`（4096 窗口下 3686，比装配器真正能装的
    有效预算 3482 还高——但装配器给非必留区段的配额合计就有 2784），于是
    「需求体积」在历史只装了几百 token 时就过线，重置提前发生、已读到的工具
    输出被整段丢掉。真机对照 36 次里 17 次如此（逐回合现场见
    `benchmarks/probes/orchestration/p0-turns.json`）。

    **这条用覆盖把旧口径搬回来当变异**（`hard_trigger_ratio=0.96` → 3342），
    并断言：`resets == 0` 且下一次请求里仍有上一次读到的正文。它的分量在
    `tests/cli/test_prompt_for_turn.py` 那两条端到端上也可以复核——那两条在
    改动前**红**，改完**绿**（见 `P0-mutations.md` 的 M6）。
    """
    body = "关键结论在这里\n" + "行\n" * 100
    (tmp_path / "a.txt").write_text(body, encoding="utf-8")
    loop = _build(
        tmp_path,
        [
            _turn("读", [_read()]),
            _turn("看完了", final="里面有内容"),
        ],
        overrides={"hard_trigger_ratio": 0.96},
    )
    result = loop.run("看看 a.txt")

    assert result.resets == 0, f"历史还装得下，不该重置：{result.trace}"
    # 第二次请求里带着上一次读到的正文（前几行就够证明它没被丢掉）。
    assert "关键结论在这里" in _last_prompt(loop)
    assert result.finished is True


def test_第二次相同调用被判成无用重复并被硬拦(tmp_path: Path) -> None:
    """**参数一字未改 + 内容指纹没变 + 上一次结果还在上下文里** → 第一次就拦。

    这条以前是「提醒一句、照常执行」。实测的代价很具体：探针 merge-html 与
    fix-bug 各出现一次「护栏只提醒、没拦」的重复，那一次真的又跑了一遍，
    白烧一次完整的模型生成与工具执行，而探针把它记在**空转**账上
    （`repeat_blocks_unblocked`）。判据的细节见 `_guard_repeat_decision`。
    """
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

    assert executed == ["read_file"], "无用重复不该真的跑第二遍"
    prompt = _last_prompt(loop)
    # 拦下的话术必须说清「为什么这次不一样」，并且带 §2.5 的反向条款。
    assert "参数一字未改" in prompt
    assert "没有执行" in prompt
    assert "一字未变" in prompt
    assert "§2.5 反向条款" in prompt


def test_判据二成立时放行并标注理由(tmp_path: Path) -> None:
    """**中间有改动真的落成** → 有理由的重取：放行，并写进 trace（不算空转）。

    这就是「刚写完再读一遍」那一类合法重取：旧结果描述的是旧文件。判据不许
    为了压指标把正常路径拦死，所以它必须真的执行，而且要留下理由。
    """
    (tmp_path / "a.txt").write_text("旧内容\n", encoding="utf-8")
    pending = PendingChanges(tmp_path)
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path, pending))
    registry.register(replace_text_spec(tmp_path, pending))
    loop = AgentLoop(
        gateway=FakeModel(
            script=[
                _turn("先看", [_read()]),
                _turn(
                    "改它",
                    [
                        {
                            "name": "replace_text",
                            "arguments": {
                                "path": "a.txt",
                                "old": "旧内容",
                                "new": "新内容",
                            },
                        }
                    ],
                ),
                _turn("再看一遍确认", [_read()]),
                _turn("好了", final="改完了"),
            ],
            tokenizer=OfflineTokenCounter(),
        ),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(
            project_root=tmp_path, context_window=4096, max_steps=8, supervise=False
        ),
        pending=pending,
    )
    reads: list[str] = []
    real = loop.registry.invoke

    def counting(call):
        if call.name == "read_file":
            reads.append(call.name)
        return real(call)

    loop.registry.invoke = counting
    result = loop.run("改一下 a.txt 再看一遍")

    assert len(reads) == 2, "内容变了的重取必须放行"
    assert any("重复调用放行" in line for line in result.trace), result.trace
    assert any("这中间有改动真的落成了" in line for line in result.trace), result.trace
    # 放行的话术必须进模型可见文本：它得知道这次为什么被允许。
    assert "⟳ 重复调用放行：" in _last_prompt(loop)


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
    loop.run("看看 a.txt")

    assert REPEAT_BLOCK_AT == 3
    # 第二次起就是无用重复（判据见 _guard_repeat_decision），所以只有第一次执行。
    assert executed == ["read_file"]
    assert "没有执行" in _last_prompt(loop)


def test_中间的调用改过文件时重读被放行(tmp_path: Path) -> None:
    """读文件 → **改文件** → 再读同一个文件：内容变了，不许拦。

    这是「不许为了压指标把正常路径拦死」那条硬要求的用例：判据只认事实
    （内容指纹变了 / 中间有改动真的落成），不认「连续几次」。
    """
    (tmp_path / "a.txt").write_text("旧\n", encoding="utf-8")
    pending = PendingChanges(tmp_path)
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path, pending))
    registry.register(write_file_spec(tmp_path, pending))
    loop = AgentLoop(
        gateway=FakeModel(
            script=[
                _turn("读", [_read()]),
                _turn("改", [{"name": "write_file", "arguments": {"path": "a.txt", "content": "新\n"}}]),
                _turn("再读", [_read()]),
                _turn("完成", final="好"),
            ],
            tokenizer=OfflineTokenCounter(),
        ),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(project_root=tmp_path, context_window=4096, max_steps=8, supervise=False),
        pending=pending,
    )
    reads: list[str] = []
    real = loop.registry.invoke

    def counting(call):
        if call.name == "read_file":
            reads.append(call.name)
        return real(call)

    loop.registry.invoke = counting
    loop.run("改完再看一眼")

    assert len(reads) == 2
    # 中间那一步是写，不是「同一件事又来一遍」，所以不许出现「与上一步完全相同」。
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

    # 每个文件最多真正读 2 次，第三次起被拦（本轮起第二次即判无用重复）
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


# ---- guard_refresh 的**实测归类**：有理由的重取 vs 无理由的重复 ----------------
#
# 这一组是用户点名要的：`guard_refresh` 不许停留在「判断」上。两条用例各跑
# 一遍真正的循环，然后从**事件流**（`on_event` 收到的东西）里读读数——
# 不是从模型的自述里读，也不是从内部标志里读。


def _collect_events(loop: AgentLoop) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    loop.on_event = lambda kind, data: events.append((kind, data))
    return events


def test_有理由的重取走guard_refresh且不算空转(tmp_path: Path) -> None:
    """内容/状态真的变了 → 事件流里是 `guard_refresh`，不是「又跑了一遍」。

    判据：同一条事件流里 `guard_refresh` 至少一条，而**没有任何一条**工具输出
    带「注意：这一步与上一步完全相同」——那句才是「护栏只提醒、没拦」的标记。
    """
    (tmp_path / "a.txt").write_text("旧内容\n", encoding="utf-8")
    pending = PendingChanges(tmp_path)
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path, pending))
    registry.register(replace_text_spec(tmp_path, pending))
    loop = AgentLoop(
        gateway=FakeModel(
            script=[
                _turn("先看", [_read()]),
                _turn(
                    "改它",
                    [
                        {
                            "name": "replace_text",
                            "arguments": {
                                "path": "a.txt",
                                "old": "旧内容",
                                "new": "新内容",
                            },
                        }
                    ],
                ),
                _turn("再看一遍确认", [_read()]),
                _turn("好了", final="改完了"),
            ],
            tokenizer=OfflineTokenCounter(),
        ),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(
            project_root=tmp_path, context_window=4096, max_steps=8, supervise=False
        ),
        pending=pending,
    )
    events = _collect_events(loop)
    loop.registry.invoke = loop.registry.invoke  # 不改执行路径，只读事件
    loop.run("改一下 a.txt 再看一遍")

    refresh = [data for kind, data in events if data.get("kind") == "guard_refresh"]
    assert refresh, f"没有 guard_refresh 事件：{[k for k, _ in events]}"
    assert any(item.get("decision") == "refresh" for item in refresh), refresh
    # 放行的调用真的执行了（内容变了就该重取），而且**不是**「只提醒没拦」。
    assert not any(
        "注意：这一步与上一步完全相同" in str(data.get("detail") or "")
        for kind, data in events
        if kind == "tool"
    )


def test_无理由的重复被判成无用重复并硬拦(tmp_path: Path) -> None:
    """内容一字未变 + 参数逐字相同 → 第一次重复就硬拦 + 换路。

    判据：工具事件里出现 `⛔ 未执行`（拦的是「这一次动作」，带反向条款），
    而且**没有** `guard_refresh`——它不是有理由的重取。
    """
    (tmp_path / "a.txt").write_text("内容\n", encoding="utf-8")
    loop = _build(
        tmp_path,
        [
            _turn("读", [_read()]),
            _turn("再读一次", [_read()]),
            _turn("算了", final="结束"),
        ],
    )
    events = _collect_events(loop)
    loop.run("看看 a.txt")

    tools = [data for kind, data in events if kind == "tool"]
    assert any("⛔ 未执行" in str(item.get("detail") or "") for item in tools), tools
    assert not [d for _, d in events if d.get("kind") == "guard_refresh"]
    # 话术必须点明「参数一字未改」，并带 §2.5 反向条款（不许被读成「禁止删除」）。
    # 反向条款在 prompt 里核（事件流的 detail 只留 80 字，尾句会被截掉）。
    assert any("参数一字未改" in str(item.get("detail") or "") for item in tools), tools
    prompt = _last_prompt(loop)
    assert "§2.5 反向条款" in prompt
    assert "参数一字未改" in prompt


def test_判不出时如实记录而且拒绝(tmp_path: Path) -> None:
    """拿不到可比对象时：**如实记成判不出，而且拒绝**（S9 起；旧口径是放行）。

    这一条钉两件事，缺一不可：
    - 事件流里必须看见「这次判不出」而且 `refused=True`（不许把「没判」记成
      「判过」）；
    - 它**不再执行**——拿不出可比对象，就没有证据说重跑有意义，而重跑要付一次
      完整的模型生成与一次工具执行。

    现场形状：`calc` 只带一个表达式，既没有正文、也没有可读路径、也不是一条
    命令，三条指纹都取不到；中间没有任何改动落成。旧口径（判不出 = 放行）让所有
    这类调用拿到免检通道，S9 关掉的就是它。
    """
    registry = ToolRegistry()
    registry.register(calc_spec())
    call = {"name": "calc", "arguments": {"expression": "1 + 1"}}
    loop = _edit_loop(
        tmp_path,
        [
            _turn("算一遍", [call]),
            _turn("再算一遍", [call]),
            _turn("好了", final="算完了"),
        ],
        registry=registry,
        max_steps=6,
    )
    events: list[tuple[str, dict]] = []
    loop.on_event = lambda kind, data: events.append((kind, data))
    ran: list[str] = []
    real = loop.registry.invoke

    def counting(call_):
        ran.append(call_.name)
        return real(call_)

    loop.registry.invoke = counting
    loop.run("算两遍同样的东西")

    assert ran == ["calc"], "判不出不再等于放行"
    undecided = [
        data for kind, data in events if data.get("kind") == "guard_repeat_undecided"
    ]
    assert undecided, f"判不出的事件没有记下来：{[k for k, _ in events]}"
    assert any(item.get("decision") == "undecided" for item in undecided), undecided
    assert any(item.get("refused") is True for item in undecided), undecided
    assert not any(item.get("capped") for item in undecided), "这条不是被上限挡住的"
    tools = [data for kind, data in events if kind == "tool"]
    assert any("⛔ 未执行" in str(item.get("detail") or "") for item in tools), tools
    prompt = _last_prompt(loop)
    assert "没有可比较的内容对象" in prompt
    assert "出路有三条" in prompt
    assert "§2.5 反向条款" in prompt


def test_同一条命令现场没变时第二次不再执行(tmp_path: Path) -> None:
    """`run_command` 现在也有可比对象：**这条命令本身**（命令 + 工作目录 + 参数）。

    S9 之前命令没有指纹，判据落到「判不出 → 放行」，于是同一条命令在没改过任何
    东西的情况下可以一路重跑。现在：参数逐字相同 + 现场（待确认清单）也没变 →
    判成无用重复，第一次重复就拒绝，并把**上一次那条命令的输出摘要**端回去，
    让模型知道自己手上那一份是哪一份。

    对照组是同文件里的 `test_回归靶_fix_bug的重跑测试被判成有理由的重取`：
    中间真的改过文件时，命令重跑仍然放行。
    """
    (tmp_path / "a.py").write_text("x = 0\n", encoding="utf-8")
    (tmp_path / "test_gate.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    pending = PendingChanges(tmp_path)
    registry = ToolRegistry()
    registry.register(run_command_spec(tmp_path, pending))
    command = {
        "name": "run_command",
        "arguments": {"command": ["pytest", "-q", "test_gate.py"]},
    }
    loop = _edit_loop(
        tmp_path,
        [
            _turn("先跑一遍", [command]),
            _turn("再跑一遍", [command]),
            _turn("好了", final="跑完了"),
        ],
        registry=registry,
        pending=pending,
        max_steps=6,
    )
    events: list[tuple[str, dict]] = []
    loop.on_event = lambda kind, data: events.append((kind, data))
    ran: list[str] = []
    real = loop.registry.invoke

    def counting(call):
        if call.name == "run_command":
            ran.append(call.name)
        return real(call)

    loop.registry.invoke = counting
    loop.run("跑两遍同一条命令")

    assert ran == ["run_command"], "同一条命令、现场没变，第二次不该真的再跑"
    prompt = _last_prompt(loop)
    assert "这条命令（命令 + 工作目录 + 参数）" in prompt
    assert "一字未变" in prompt
    assert "上一次那条命令的输出摘要" in prompt
    assert "§2.5 反向条款" in prompt
    # 判不出的事件不该出现：这一条是**判过**的（判成无用重复）。
    assert not [d for _, d in events if d.get("kind") == "guard_repeat_undecided"]


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


def _edit_loop(
    tmp_path: Path, script: list, registry=None, pending=None, **kwargs
) -> AgentLoop:
    if pending is None:
        pending = PendingChanges(tmp_path)
    if registry is None:
        registry = ToolRegistry()
        registry.register(write_file_spec(tmp_path, pending))
    settings = {"max_steps": 12, **kwargs}
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=OfflineTokenCounter()),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(
            project_root=tmp_path,
            context_window=4096,
            supervise=False,
            **settings,
        ),
        pending=pending,
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
    """`failed_repeat_block_at` 是可覆盖的：调到 3 就允许它再试一次。

    S9 之后这条用例多了一层意思：失败之后原样重提**不再**走「判不出」那条
    （那条现在会被拒），而是被判成「有理由的重取——上一次执行失败了」。
    所以两件事都得钉：第二次真的执行了，而且理由写的是重试；第三次被那道闸
    拦下（`failures >= block_at - 1`）。若放行路不记失败，这个阈值就数不到
    重试，会变成「允许无限重试」。
    """
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
    events: list[tuple[str, dict]] = []
    loop.on_event = lambda kind, data: events.append((kind, data))
    executed: list[str] = []
    real = loop.registry.invoke

    def counting(call):
        if call.name == "write_file":
            executed.append(call.name)
        return real(call)

    loop.registry.invoke = counting
    result = loop.run("合并成一份单文档")
    # 阈值 3 = 允许一次重试；第二次重提才拦。
    assert len(executed) == 2
    refreshes = [d for _, d in events if d.get("kind") == "guard_refresh"]
    assert any("上一次执行失败了" in str(item.get("reason") or "") for item in refreshes), (
        refreshes
    )
    assert any("重试" in line for line in result.trace)


def test_步数预算不放大的时候硬上限就是配置的值(tmp_path: Path) -> None:
    """P0 修①：**预算就是预算**——默认配置下「按目标行数放大」一次都不许发生。

    现场（真机对照 36 次，HEAD 4906942）：配 12 步的格子里有 10 格实跑到
    13–24 步（`step_lines_per_bonus` 被模型反复重写大文件触发），而读数里
    只写「未完成」——上限被抬过这件事一个字都没有。

    所以默认 `step_budget_hard_cap=0`：脚本给 11 步编辑 + 收尾，而预算只有
    10 步，于是它在第 10 步撞墙、**收不了尾**，步数**不超过** 10。这正是旧行为里
    被放大掩盖掉的那条线。

    S8 把「收束期拦写」做成默认之后，这条用例的**收尾那半**换了形状，值得写下来：
    最后两步的 `write_file` 被拒（`converge_write_grace=0`），而**被硬拦的调用
    不占步数预算**（这条规则比 S8 早，见 trace 里的「已退还 N/3」），于是脚本在
    被拒两次之后仍然走到了那条收尾应答、`finished` 变成 True。所以这里不再钉
    `finished is False`（那是「脚本耗尽」的代理指标，不是预算语义），改钉三件
    真正该成立的事：**步数没超预算、被拒的写一次都没落盘、被拒的调用确实走了
    「不占步数预算」那条路**。
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
                        # 所以每次都够得着「按规模放大」那条。
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

    assert result.steps <= 10, f"配置 10 步却跑了 {result.steps} 步"
    # 放大**被夹住**这件事必须在 trace 里看得见（读数里能看出有没有放大）。
    assert any("硬上限 10 步——实际 10 步（放大被夹住）" in line for line in result.trace), (
        result.trace
    )
    # 收束期那两次写被拒（默认额度 0），而且它们**不占步数预算**——
    # 拦动作不拦成果：拒的是动作，代价不该记在模型的步数上。
    assert sum("拒掉写成果" in line for line in result.trace) == 2, result.trace
    assert any("不占步数预算" in line for line in result.trace), result.trace
    # 被拒的两次**一次都没执行**：脚本给了 11 次改写，真正执行到的只有前 9 次
    # （第 9、10 次落在收束期，被额度挡在门外）。
    ok_writes = [
        line for line in result.trace if "工具 write_file" in line and "成功" in line
    ]
    assert len(ok_writes) == 9, result.trace
    assert not any("改第 9 处" in line or "改第 10 处" in line for line in ok_writes), ok_writes



def test_显式放宽硬上限之后放大才生效且仍然有界(tmp_path: Path) -> None:
    """`step_budget_hard_cap` 是**唯一的放宽入口**，而且它自己也是上界。

    两条一起证：① 显式放宽到 5 步之后，同一份脚本跑得完（放大真的生效）；
    ② 放宽到 5 步时，即使 `step_lines_bonus_cap` 允许补 12 步，
    实际生效的上限也只有 15（= 10 + 5）——放大不会越过这条线。
    """
    from spoolkit.agent.loop import clamp_step_limit, step_hard_cap

    config = Config(
        project_root=tmp_path,
        context_window=4096,
        max_steps=10,
        supervise=False,
        overrides={"step_budget_hard_cap": 5},
    )
    assert step_hard_cap(config) == 15
    # 想放大到 22（= 10 + 12）也被夹在 15。
    assert clamp_step_limit(config, 22) == 15

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
        config=config,
    )
    result = loop.run("改一份 300 行的文件")

    assert result.steps <= 15, result.steps
    assert result.finished is True, "显式放宽 5 步之后预算够收尾了"
    assert any("步数预算按规模放大到 15（硬上限 15 步）" in line for line in result.trace), (
        result.trace
    )


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


# ---- 两条真病态的回归靶（闸门 1 最新读数里扣分的全部原因）-------------------
#
# 现场（`benchmarks/probes/stability/set7-t0-l3.json` 与
# `set9-t07-s2-l3.json`）：
#
#   merge-html r0 @t=0 ：`repeat_blocks_unblocked=1`、`blocked=2`，判据绿；
#   fix-bug   r1 @t=0.7：`repeat_blocks_unblocked=1`，判据绿。
#
# 两条都不是「任务做不成」，而是**护栏只提醒、没拦住的那一次真的又跑了一遍**
# ——白烧一次完整的模型生成与工具执行。所以这两条用例的读数是**两个**：
# ① 那次重复不再执行（未拦的重复归零）；② 任务仍然能完成（不许为了压指标
# 把正常路径拦死）。


def test_回归靶_merge_html的重复写入不再白跑一遍(tmp_path: Path) -> None:
    """现场形状：write_file 暂存了正文 → 同一个调用一字不改地又发一遍。

    第一次的正文进了暂存区（`STAGE_MARKER`），它**不是失败**，所以第二次不是
    「失败重提」那条闸能拦的；旧行为只提醒一句、照常执行，结果第二次拿到的是
    「内容与现有完全一致，没有产生改动」——一次纯空转。新行为按无效重复硬拦。

    正文规模刻意贴近原文件：远大于原文件的整份重写走的是另一条闸
    （`⚠ 拒绝整文件重写`，砍半护栏），那会把这条用例测成别的东西。
    """
    (tmp_path / "index.html").write_text(
        "<html>\n<head><title>首页</title></head>\n<body>\n"
        + "\n".join(f"<p>老内容 {i}</p>" for i in range(40))
        + "\n</body>\n</html>\n",
        encoding="utf-8",
    )
    pending = PendingChanges(tmp_path)
    registry = ToolRegistry()
    registry.register(write_file_spec(tmp_path, pending))
    body = (
        "<html>\n<head><title>首页</title></head>\n<body>\n"
        + "\n".join(f"<p>价格方案 {i}</p>" for i in range(40))
        + "\n</body>\n</html>\n"
    )
    same = {
        "name": "write_file",
        "arguments": {"path": "index.html", "content": body, "rewrite_reason": "并入价格页"},
    }
    loop = _edit_loop(
        tmp_path,
        [
            _turn("整份重写", [same]),
            _turn("原样再发一遍", [same]),
            _turn("算了", final="先这样"),
        ],
        registry=registry,
        pending=pending,
        max_steps=8,
    )
    executed: list[str] = []
    real = loop.registry.invoke

    def counting(call):
        if call.name == "write_file":
            executed.append(call.name)
        return real(call)

    loop.registry.invoke = counting
    loop.run("把价格页并进首页")

    assert executed == ["write_file"], "暂存过的那次写入不该再跑一遍（那就是空转）"
    assert "没有执行" in _last_prompt(loop)
    # 任务仍然完成：暂存的正文还在，收尾报告里有可回退的改动。
    assert pending.items(), "第一次的正文必须已经进了暂存区"


def test_回归靶_fix_bug的重跑测试被判成有理由的重取(tmp_path: Path) -> None:
    """现场形状：跑测试（红）→ 改文件 → **同一条**测试命令再跑一次（绿）。

    参数逐字相同，第二次却是**有理由的重取**：中间有改动真的落成了，重跑才能
    看到 1→0 的退出码变化。所以它必须放行，并被标注成「判过」而不是「空转」。
    这条用例就是「不许为了压指标把正常路径拦死」那一条的回归靶。
    """
    (tmp_path / "a.py").write_text("x = 0\n", encoding="utf-8")
    (tmp_path / "test_gate.py").write_text(
        "import pathlib\n\n\ndef test_文件还在():\n"
        "    assert pathlib.Path('a.py').exists()\n",
        encoding="utf-8",
    )
    pending = PendingChanges(tmp_path)
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path, pending))
    registry.register(replace_text_spec(tmp_path, pending))
    registry.register(list_dir_spec(tmp_path, pending))
    registry.register(run_command_spec(tmp_path, pending))
    # 真跑一条白名单命令（pytest），而且**第一次是绿的**：只有成功执行过的
    # 调用才有「上一次的结果仍在上下文里」这件事可谈。第二次才是被判的那一次。
    command = {"name": "run_command", "arguments": {"command": ["pytest", "-q", "test_gate.py"]}}
    loop = _edit_loop(
        tmp_path,
        [
            _turn("先跑一遍看现状", [command]),
            _turn(
                "修掉",
                [
                    {
                        "name": "replace_text",
                        "arguments": {"path": "a.py", "old": "x = 0", "new": "x = 1"},
                    }
                ],
            ),
            _turn("再跑一遍确认", [command]),
            _turn("好了", final="修完了"),
        ],
        registry=registry,
        pending=pending,
        max_steps=8,
    )
    events: list[tuple[str, dict]] = []
    loop.on_event = lambda kind, data: events.append((kind, data))
    ran: list[str] = []
    real = loop.registry.invoke

    def counting(call):
        if call.name == "run_command":
            ran.append(call.name)
        return real(call)

    loop.registry.invoke = counting
    result = loop.run("修一下 a.py，测试要全过")

    assert ran == ["run_command", "run_command"], "中间改过文件，重跑必须放行"
    assert any("重复调用放行" in line for line in result.trace), result.trace
    # 放行的事实进事件流（`guard_refresh`），而不是「只提醒没拦」那一句。
    assert any(data.get("kind") == "guard_refresh" for _, data in events)
    assert not any(
        "注意：这一步与上一步完全相同" in str(data.get("detail") or "")
        for kind, data in events
        if kind == "tool"
    )
    assert result.finished is True


def test_回归靶_单回合15个list_dir不再逐个执行(tmp_path: Path) -> None:
    """真机现场：`A-todo|t0.0|on` 第 28 个回合**一口气发了 15 个 list_dir**。

    改动前 `_call_state` 只认得到「文件正文」：目录没有正文（`_text_of` 对目录
    返回 None），于是这 15 个调用每次都拿空指纹 → 三分类只能归成「判不出」→
    逐个真的执行。现在目录清单也进指纹：5 个目录各执行一次，重复的 10 次当场
    拦下。这条是「真机 ×15」那个现场的回归靶。
    """
    dirs = ["pkg", "src", "docs", "tests", "tools"]
    for name in dirs:
        (tmp_path / name).mkdir()
        (tmp_path / name / "a.py").write_text("x = 1\n", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(list_dir_spec(tmp_path))
    burst = [{"name": "list_dir", "arguments": {"path": one}} for one in dirs * 3]
    loop = _edit_loop(
        tmp_path,
        [_turn("一口气列一圈", burst), _turn("好了", final="看完了")],
        registry=registry,
    )
    ran: list[str] = []
    real = loop.registry.invoke

    def counting(call):
        ran.append(str(call.arguments.get("path")))
        return real(call)

    loop.registry.invoke = counting
    events = _collect_events(loop)
    result = loop.run("看看项目里有什么")

    assert sorted(ran) == sorted(dirs), f"每个目录只该真的列一次，实际执行了 {ran}"
    assert any("重复调用第 2 次" in line for line in result.trace), result.trace
    # 话术要承认这次比的是**目录清单**，不能让模型去找一个不存在的「文件内容」。
    # 这里从事件流而不是最后一轮 prompt 里读：这一轮的 15 条输出本身就可能把
    # 上下文顶到硬线（下面 §2 的素材卡会接手），拿 prompt 断言等于在测预算。
    assert any(
        "目录清单" in str(data.get("detail") or "")
        for kind, data in events
        if kind == "tool"
    ), events
    assert result.finished is True


def test_回归靶_同一个confirm_rewrite连发只执行一次(tmp_path: Path) -> None:
    """真机现场：一个 run 里 `confirm_rewrite` 被连发了 18 次。

    这个工具的参数**只有令牌与逐项交代**（没有正文、没有路径），改动前
    `_call_state` 拿不到任何可比对象 → 三分类永远归成「判不出」→ 每一次都真的
    执行。现在「令牌指向的那份暂存记录」本身进了指纹：一模一样的一串确认只
    执行第一次；把项 id 补进 `dropped` 的那一次是**另一次尝试**，照样放行并
    真的落盘——回归靶必须同时钉住这两半，否则就成了「为了压重复把正常路径
    拦死」。
    """
    old = "".join(f'<p id="p{index}">第 {index} 行</p>\n' for index in range(12))
    new = '<html>\n<p id="p0">第 0 行</p>\n</html>\n'
    (tmp_path / "snake.html").write_text(old, encoding="utf-8")
    pending = PendingChanges(tmp_path)
    staged = write_file_spec(tmp_path, pending).handler(
        {"path": "snake.html", "content": new, "rewrite_reason": "合并重复段落"}
    )
    assert staged.content.startswith("⏸"), staged.content[:200]
    token = pending.staged_items()[0].token
    record, why = pending.lookup_staged(token)
    assert record is not None, why
    ids = record.ids()
    assert len(ids) >= 3, ids

    same = {"name": "confirm_rewrite", "arguments": {"token": token}}
    fixed = {
        "name": "confirm_rewrite",
        "arguments": {
            "token": token,
            "dropped": [f"{one}：重复段落，合并时去掉了" for one in ids],
        },
    }
    registry = ToolRegistry()
    registry.register(write_file_spec(tmp_path, pending))
    registry.register(confirm_rewrite_spec(tmp_path, pending))
    loop = _edit_loop(
        tmp_path,
        [_turn("确认", [same]) for _ in range(6)]
        + [_turn("逐项交代", [fixed]), _turn("好了", final="确认完了")],
        registry=registry,
        pending=pending,
    )
    ran: list[str] = []
    real = loop.registry.invoke

    def counting(call):
        if call.name == "confirm_rewrite":
            ran.append(str(call.arguments.get("token")))
        return real(call)

    loop.registry.invoke = counting
    result = loop.run("把 snake.html 合并成一份")

    assert len(ran) == 2, f"6 次一样的确认只该执行 1 次、改了说法的那次放行，实际 {ran}"
    assert ran[0] == ran[1] == token
    prompt = _last_prompt(loop)
    assert "完全相同" in prompt, prompt
    assert "没有执行" in prompt, prompt
    # 改了说法的那一次真的落成了：成果进了待确认清单（`apply` 才写盘），
    # 不在「被拦」里消失。
    landed = pending.items()
    assert [one.path for one in landed] == ["snake.html"], landed
    assert landed[0].new_text == new
    assert pending.staged_items() == []
    assert result.finished is True
