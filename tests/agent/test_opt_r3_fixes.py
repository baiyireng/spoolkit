"""复跑「优化一下旧文件的代码」（第三轮）逼出来的五处修。

用户的轨迹里有一串互相咬住的毛病，这里一条一条钉住：

A. **拦读造成的死锁**：重复调用护栏只看「签名在最近几步里出现过几次」，
   不看「上一份结果还在不在上下文里」。上下文被压掉之后，它把手上一无所有
   的模型拦在 read_file 外面——「只有一个被拦的 read、别的啥也没有」。
B. **行号漂移把文件改坏**：连续两次编辑同一个文件时，第二次的行号是旧的，
   而旧实现读磁盘校验、写磁盘内容，锚点对不上也照样通过。
C. **督导在事实层面说错话**：「尝试使用不存在的 confirm_rewrite 工具」
   （那把工具就在注册表里）、「自动验证通过（0 处改动）」（明明有改动）。
D. **收手与落盘抢跑**：收手判词先出，结论里看不到最终文件的真实状态。
E. **意图要含「想拿什么 + 接下来怎么做」**（提示词一侧，见
   tests/test_prompt_mechanics.py 的锚点核对）。

每条都配了「变异会红」的判据：把对应的判断去掉，这里就挂。
"""

import json
from pathlib import Path

from spoolkit.agent.loop import (
    GUARD_REFRESH_PREFIX,
    REPEAT_REFRESH_PREFIX,
    AgentLoop,
    call_signature,
    _REFRESH_COMPACTED_LIMIT,
    _REFRESH_LIMIT,
)
from spoolkit.agents.supervisor import (
    STOP,
    RunFacts,
    Verdict,
    fact_conflicts,
    sanitize_verdict,
)
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.edit import PendingChanges, replace_lines_spec, write_file_spec
from spoolkit.tools.fs import read_file_spec
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.types import ToolResult


def _turn(thought: str, calls=None, final=None, assessment: str = "") -> str:
    return json.dumps(
        {
            "thought": thought,
            "tool_calls": calls or [],
            "state": None,
            "done": final is not None,
            "final": final,
            "assessment": assessment,
        },
        ensure_ascii=False,
    )


def _read(path: str, **extra) -> dict:
    return {"name": "read_file", "arguments": {"path": path, **extra}}


def _build(tmp_path: Path, script: list, **kwargs) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path))
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


def _last_prompt(loop: AgentLoop) -> str:
    return "\n".join(m.content for m in loop.gateway.requests[-1].messages)


def patch_guard(monkeypatch, loop: AgentLoop, hook) -> None:
    """在**类**上打补丁。

    不能在实例上打：循环内部走的是 `self._invoke_guarded(...)`，实例属性
    不会参与那次查找（第一版就是这么写的，补丁根本没生效——而这正是这些
    用例要盯的那类「看起来做了、其实没做」）。
    """
    real = AgentLoop._invoke_guarded

    def wrapped(self, call, repeats, seen=1):
        hook(call)
        return real(self, call, repeats, seen)

    monkeypatch.setattr(AgentLoop, "_invoke_guarded", wrapped)


# ---- A：结果已被压掉 / 文件已变 → 不该硬拦 ---------------------------------


def test_第三次重复读被拦是因为结果还在上下文里(tmp_path: Path) -> None:
    """基线行为不变：内容还在时照样硬拦（护栏没有整体松掉）。

    本轮把这条线**前移**到第一次重复：参数一字未改 + 内容指纹没变 +
    上一次结果还在上下文里，就是无用重复，第二次进来就该拦，不必等第三次。
    第三次进来仍然是被拦（判据没变，只是更早生效）。
    """
    (tmp_path / "a.txt").write_text("内容\n", encoding="utf-8")
    loop = _build(
        tmp_path,
        [_turn("读", [_read("a.txt")]), _turn("再读", [_read("a.txt")]),
         _turn("还读", [_read("a.txt")]), _turn("算了", final="结束")],
    )
    executed: list[str] = []
    real = loop.registry.invoke

    def counting(call):
        executed.append(call.name)
        return real(call)

    loop.registry.invoke = counting
    result = loop.run("看看 a.txt")

    assert executed.count("read_file") == 1, "内容还在上下文里时第二次起就该被拦"
    assert (
        any("一字未变" in line for line in result.trace)
        or "一字未变" in _last_prompt(loop)
    )
    assert not any(GUARD_REFRESH_PREFIX in line for line in result.trace)


def test_内容被压缩掉之后再读同一个文件不被硬拦(
    tmp_path: Path, monkeypatch
) -> None:
    """用户点名的死锁现场：读 → 上下文被压掉 → 再读，第三次不该被硬拦。

    判据不是签名计数，而是事实：上一份结果**已经不在上下文里**了
    （`_context_revision` 变过）。变异：去掉 `_guard_repeat_decision` 的
    第一条判据，这条会红——第三次读又变成「没有执行」，而模型手上什么都没有。

    读数要说准：第二次（内容没变、什么都没落成）是**无用重复**，现在就在
    那一次硬拦；第三次因为版本推进而被判成有理由的重取，所以真的执行。
    两次执行的差别正是这一轮修的东西。
    """
    (tmp_path / "a.txt").write_text("内容\n", encoding="utf-8")
    loop = _build(
        tmp_path,
        [_turn("读", [_read("a.txt")]), _turn("再读", [_read("a.txt")]),
         _turn("还读", [_read("a.txt")]), _turn("算了", final="结束")],
    )
    calls = {"n": 0}

    def hook(call):
        calls["n"] += 1
        # 进入第三次调用之前先把上下文版本推一格：这模拟真实的时序——
        # 压缩发生在装配上下文那一步（调用**之前**），于是第三次进来时
        # 「上一次的结果已经被砍掉」这件事已经成立了。
        if calls["n"] == 3:
            loop._context_revision += 1

    patch_guard(monkeypatch, loop, hook)
    executed: list[str] = []
    real = loop.registry.invoke

    def counting(call):
        executed.append(call.name)
        return real(call)

    loop.registry.invoke = counting
    result = loop.run("看看 a.txt")

    # 第一次执行 + 第三次（版本推进 → 有理由的重取）执行；第二次被硬拦。
    assert executed.count("read_file") == 2, "内容已被压掉时第三次必须能再取一次"
    assert any(REPEAT_REFRESH_PREFIX in line for line in result.trace) or any(
        GUARD_REFRESH_PREFIX in line for line in result.trace
    ), result.trace
    assert any("已经被上下文压缩丢弃" in line for line in result.trace)
    # 第三次不是硬拦：它的回执是「放行 + 重取了一遍」，而不是「没有执行」。
    # （不能拿整份 prompt 找「没有执行」——第二步那次硬拦的话术还在历史里，
    # 那是上一轮的记录，不是这一轮的处置。）
    assert "这一次重新取了一遍" in _last_prompt(loop)


def test_刷新读取会给出紧凑的文件状态(tmp_path: Path, monkeypatch) -> None:
    """被拦/放行都必须有替代途径：这里是行数 + 指纹的紧凑摘要，不是整份内容。"""
    (tmp_path / "a.txt").write_text("内容\n", encoding="utf-8")
    loop = _build(
        tmp_path,
        [_turn("读", [_read("a.txt")]), _turn("再读", [_read("a.txt")]),
         _turn("还读", [_read("a.txt")]), _turn("算了", final="结束")],
    )
    calls = {"n": 0}

    def hook(call):
        calls["n"] += 1
        if calls["n"] == 3:
            loop._context_revision += 1

    patch_guard(monkeypatch, loop, hook)
    loop.run("看看 a.txt")

    prompt = _last_prompt(loop)
    assert "a.txt 现在是" in prompt
    assert "指纹" in prompt


def test_版本判据单独成立也放行(tmp_path: Path, monkeypatch) -> None:
    """只有版本变了（文件没变）时同样放行——第一条判据的单独覆盖。

    与下面那条分开的理由：两条判据各自都要有「只有它成立」的用例，
    否则删掉其中一条时另一条会替它兜住，变异就抓不出来。
    """
    target = tmp_path / "a.txt"
    target.write_text("第一版\n", encoding="utf-8")
    loop = _build(
        tmp_path,
        [_turn("读", [_read("a.txt")]), _turn("再读", [_read("a.txt")]),
         _turn("还读", [_read("a.txt")]), _turn("算了", final="结束")],
    )
    calls = {"n": 0}

    def hook(call):
        calls["n"] += 1
        if calls["n"] == 3:
            # **不改文件、只推版本**：这样只有第一条判据能成立，
            # 第二条（调用对象指纹）在这条用例里恰好为假。
            loop._context_revision += 1

    patch_guard(monkeypatch, loop, hook)
    result = loop.run("看看 a.txt")

    assert any(REPEAT_REFRESH_PREFIX in line for line in result.trace) or any(
        GUARD_REFRESH_PREFIX in line for line in result.trace
    ), result.trace
    assert any("已经被上下文压缩丢弃" in line for line in result.trace)


def test_文件改过_内容判据不成立时也能放行(tmp_path: Path, monkeypatch) -> None:
    """只有文件变了、上下文没变时，也要放行。

    这条单独钉住第二条判据（内容指纹）：变异时把
    `_guard_repeat_decision` 里那一条删掉，上一条用例仍会因为版本判据而绿，
    这一条必红。
    """
    target = tmp_path / "a.txt"
    target.write_text("第一版\n", encoding="utf-8")
    loop = _build(
        tmp_path,
        [_turn("读", [_read("a.txt")]), _turn("再读", [_read("a.txt")]),
         _turn("还读", [_read("a.txt")]), _turn("算了", final="结束")],
    )
    calls = {"n": 0}

    def hook(call):
        calls["n"] += 1
        if calls["n"] == 3:
            # 只改文件：指纹变了，而 `_seen_at_revision` 仍是当前版本。
            target.write_text("第二版\n", encoding="utf-8")

    patch_guard(monkeypatch, loop, hook)
    result = loop.run("看看 a.txt")
    assert any(REPEAT_REFRESH_PREFIX in line for line in result.trace) or any(
        GUARD_REFRESH_PREFIX in line for line in result.trace
    ), result.trace
    assert any("与上次执行时不一样" in line for line in result.trace), result.trace


def test_刷新读取有上限(tmp_path: Path, monkeypatch) -> None:
    """放行刷新是为了不死锁，不是为了把预算变成无限。

    这条量的是「结果被上下文压掉」这一本账的份额（D6）：它单独更小
    （`_REFRESH_COMPACTED_LIMIT`），因为它在病态轨迹里**每一步都成立**
    （读一次丢一次），会把整份额度吃光。总额度另有一条全局上限，
    两者**都不放宽**——这里同时把总数也钉住。
    """
    (tmp_path / "a.txt").write_text("内容\n", encoding="utf-8")
    script = [_turn(f"第{i}次读", [_read("a.txt")]) for i in range(12)]
    script.append(_turn("算了", final="结束"))
    loop = _build(tmp_path, script)

    def hook(call):
        loop._context_revision += 1

    patch_guard(monkeypatch, loop, hook)
    result = loop.run("一直读")

    refreshes = [
        line
        for line in result.trace
        if REPEAT_REFRESH_PREFIX in line or GUARD_REFRESH_PREFIX in line
    ]
    # 上界要**真的被用到**才算测到：只断言 `<= N` 的话，把放行逻辑整个删掉
    # （一次都不放行）也是绿的——那是「上界生效」与「功能没了」分不清。
    assert refreshes, "刷新读取一次都没发生，上界这条断言就没有意义"
    # 压掉这一本：恰好用满份额，一次都不多。
    assert len(refreshes) == _REFRESH_COMPACTED_LIMIT, (
        f"「结果被压掉」这本账的份额不对：{len(refreshes)}"
    )
    assert loop._refreshes_by_cause == {"compacted": _REFRESH_COMPACTED_LIMIT}
    # 全局上限一分没放宽：总数仍受 `_REFRESH_LIMIT` 管，且**还剩一次**
    # ——那一次是留给"重取真能拿到新内容"的（见 test_probe_regression 里
    # `test_压缩账用完后内容真变了仍放行`）。
    assert loop._refreshes_used == _REFRESH_COMPACTED_LIMIT
    assert loop._refreshes_used < _REFRESH_LIMIT, "总额度被分账偷偷放大了"


# ---- B：行号漂移 -----------------------------------------------------------


def test_不带锚点的按行替换会被拒绝(tmp_path: Path) -> None:
    """纯行号不再放行。变异：把 expect 的必填检查去掉，这条会红。"""
    (tmp_path / "a.py").write_text("l1\nl2\nl3\n", encoding="utf-8")
    pending = PendingChanges(tmp_path)
    result = replace_lines_spec(tmp_path, pending).handler(
        {"path": "a.py", "start_line": 2, "end_line": 2, "content": "x\n"}
    )
    assert result.ok is False
    assert "expect" in result.content
    assert len(pending) == 0, "被拒的替换不该登记改动"


def test_锚点对不上时拒绝并给出真实内容(tmp_path: Path) -> None:
    """行号漂了而区间还在文件里 → 旧实现会安静地改错一段（这里必须拦住）。"""
    (tmp_path / "a.py").write_text("l1\nl2\nl3\nl4\n", encoding="utf-8")
    pending = PendingChanges(tmp_path)
    result = replace_lines_spec(tmp_path, pending).handler(
        {"path": "a.py", "start_line": 2, "end_line": 2, "content": "X\n",
         "expect": "l3"}
    )
    assert result.ok is False
    assert "expect 与实际内容不一致" in result.content
    assert "行号很可能已经漂了" in result.content


def test_连续两次编辑都对着当前内容校验(tmp_path: Path) -> None:
    """**用户那次把文件改坏的机制**：第二次编辑必须看到第一次的结果。

    旧实现里 `_propose` 读磁盘、`PendingChanges.propose` 也读磁盘，于是
    「同一路径以最后一次为准」= 第二次把第一次整个盖掉，而它的行号与锚点
    都是对**改动之前**的内容校验的——两次改动互相抹掉，落盘的就是残文件。

    变异：把 `_propose` 里的 `WorkspaceView(root, pending).read_text(target)`
    改回 `target.read_text()`，这条会红（第二次会把第一次的结果盖掉）。
    """
    (tmp_path / "a.py").write_text("l1\nl2\nl3\nl4\n", encoding="utf-8")
    pending = PendingChanges(tmp_path)
    spec = replace_lines_spec(tmp_path, pending)

    first = spec.handler(
        {"path": "a.py", "start_line": 2, "end_line": 2, "content": "X\n",
         "expect": "l2"}
    )
    assert first.ok is True
    # 第一次改完之后，第 3 行才是 l3。第二次拿**旧行号**（3–4）去改：
    # 视图里第 3–4 行现在是 l3、l4——锚点对上就说明它看到的是最新内容。
    # 这里刻意用一个只在「看到第一次结果」时才成立的锚点。
    second = spec.handler(
        {"path": "a.py", "start_line": 3, "end_line": 3, "content": "Y\n",
         "expect": "l3"}
    )
    assert second.ok is True
    pending.apply()
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "l1\nX\nY\nl4\n"


def test_编辑回执给出新行数与指纹(tmp_path: Path) -> None:
    """模型必须知道「行号已经失效」：回执里给新行数、新指纹、动过哪一段。"""
    (tmp_path / "a.py").write_text("l1\nl2\nl3\nl4\n", encoding="utf-8")
    pending = PendingChanges(tmp_path)
    result = replace_lines_spec(tmp_path, pending).handler(
        {"path": "a.py", "start_line": 2, "end_line": 3, "content": "new\n",
         "expect": "l2\nl3"}
    )
    assert result.ok is True
    assert "行号回执" in result.content
    assert "现在是 3 行" in result.content
    assert "指纹" in result.content
    assert "此前的行号已经失效" in result.content


def test_锚点忽略行尾空白但不忽略缩进(tmp_path: Path) -> None:
    """抄原文时多一个行尾空格很常见；缩进抄错就是抄错了内容。"""
    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    pending = PendingChanges(tmp_path)
    spec = replace_lines_spec(tmp_path, pending)
    ok = spec.handler(
        {"path": "a.py", "start_line": 2, "end_line": 2,
         "content": "    return 2\n", "expect": "    return 1   "}
    )
    assert ok.ok is True
    bad = spec.handler(
        {"path": "a.py", "start_line": 2, "end_line": 2,
         "content": "    return 3\n", "expect": "return 2"}
    )
    assert bad.ok is False


# ---- C：督导的事实性断言 ---------------------------------------------------


def test_声称工具不存在会被改写() -> None:
    facts = RunFacts(
        tools=frozenset({"confirm_rewrite", "read_file"}),
        edits=1,
        changed=frozenset({"snake.html"}),
    )
    verdict = Verdict(
        action=STOP,
        reason="step18 尝试使用不存在的 confirm_rewrite 工具",
    )
    out, conflicts = sanitize_verdict(verdict, facts)
    assert len(conflicts) == 1
    assert "与记录不符（已按运行记录改写）" in out.reason
    assert "confirm_rewrite 在本次运行的工具里" in out.reason
    # 行动不变：核对是加事实，不是顺手改判断。
    assert out.action == STOP


def test_声称零改动会被改写() -> None:
    """用户那次的真实错话：「自动验证连续失败（0 处改动），说明它没有实际修改文件」。"""
    facts = RunFacts(
        tools=frozenset({"read_file"}), edits=1, changed=frozenset({"snake.html"})
    )
    verdict = Verdict(
        action=STOP,
        reason="自动验证连续失败（0 处改动），说明它没有实际修改文件",
        message="它一处改动都没有，不要再试了",
    )
    out, conflicts = sanitize_verdict(verdict, facts)
    assert len(conflicts) == 2, "reason 与 message 里各有一条"
    assert "本次运行有 1 处改动落成（snake.html）" in out.reason
    assert "与记录不符" in out.message


def test_没有改动时不许说它改过() -> None:
    """反向也要成立：真的一处没改时，那句话是对的，不许被改写。"""
    facts = RunFacts(tools=frozenset({"read_file"}), edits=0, changed=frozenset())
    verdict = Verdict(action=STOP, reason="本次运行 0 处改动，说明它没有实际修改文件")
    out, conflicts = sanitize_verdict(verdict, facts)
    assert conflicts == ()
    assert out.reason == verdict.reason


def test_工具名大小写不同也算同一把工具() -> None:
    facts = RunFacts(tools=frozenset({"confirm_rewrite"}), edits=0)
    found = fact_conflicts("它调用了一个不存在的 Confirm_Rewrite 工具", facts)
    assert found, "大小写不同就漏判，等于把假事实端给模型"


def test_拿不到记录时不假装核对过() -> None:
    """facts 为 None 时照旧返回：核对是加强，不是新的必经环节。"""
    verdict = Verdict(action=STOP, reason="使用不存在的 confirm_rewrite 工具")
    out, conflicts = sanitize_verdict(verdict, None)
    assert conflicts == ()
    assert out is verdict


# ---- D：收手也要报最终状态 -------------------------------------------------


def _edit_loop(tmp_path: Path, script: list, **kwargs) -> tuple[AgentLoop, list[str]]:
    pending = PendingChanges(tmp_path)
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path, pending))
    registry.register(write_file_spec(tmp_path, pending))
    called: list[list[str]] = []

    def verify(changed: list[str]) -> ToolResult:
        called.append(list(changed))
        return ToolResult(ok=True, content="按产出类型检查 1 个文件：通过")

    loop = AgentLoop(
        gateway=FakeModel(script=script, tokenizer=OfflineTokenCounter()),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(
            project_root=tmp_path,
            context_window=4096,
            # 3 步脚本 = 改（1）→ 读（2）→ 宣告完成（3），所以上限要给到 4：
            # 上限判定在步首，正好用满时最后那一步会被挡在门外。
            max_steps=6,
            supervise=False,
            **kwargs,
        ),
        verify=verify,
        pending=pending,
    )
    return loop, called  # type: ignore[return-value]


def test_收手时对最终文件跑一次验证并报出真实状态(tmp_path: Path) -> None:
    """改动已经落成时，收手判词不许把它说成没发生。

    变异：把 `_with_final_state` 那两处调用去掉，这条会红。
    """
    (tmp_path / "snake.html").write_text("<html>\n</html>\n", encoding="utf-8")
    loop, called = _edit_loop(
        tmp_path,
        [
            _turn("改一下", [{"name": "write_file",
                             "arguments": {"path": "snake.html",
                                           "content": "<html>\n<body>\n</html>\n"}}]),
            _turn("再确认一下",
                  [{"name": "read_file", "arguments": {"path": "snake.html"}}]),
            _turn("看完了", final="改完了"),
        ],
    )
    result = loop.run("改一个旧文件")

    assert result.finished is True
    assert called, "收手时没有报出最终文件的验证结果"
    assert result.final.startswith("改完了")
    assert "本次运行共落成 1 处改动" in result.final
    assert "snake.html" in result.final
    assert "指纹" in result.final
    assert "最终验证：通过" in result.final
    # 这条走的是**模型自己 done**那条路（模型收的手）：
    # 两条收尾路径各报一次最终状态，另有 STOP/FINISH 两条用例分别钉住。
    assert any("沿用最近一次" in line for line in result.trace)


def test_一处改动都没有时结论保持原样(tmp_path: Path) -> None:
    """没有落盘就没有「抢跑」可言：结论保持原样，不加那段状态。

    这也是「如实」的另一半：只读角色（审查者）的结论后面不该拖一句
    「这次运行没改过任何文件」——那既没用，又会被读成系统在替它下结论。
    """
    (tmp_path / "x.txt").write_text("x\n", encoding="utf-8")
    loop, called = _edit_loop(
        tmp_path,
        [
            _turn("看", [{"name": "read_file", "arguments": {"path": "x.txt"}}]),
            _turn("看完了", final="看完了，没什么可改的"),
        ],
    )
    result = loop.run("做点什么")
    assert result.finished is True
    assert result.final == "看完了，没什么可改的"
    assert called == [], "没有改动时不该跑最终验证"


def test_督导说finish时也带上最终状态(tmp_path: Path) -> None:
    """FINISH 那条路同样是「收手」，真实状态一样要报。"""
    (tmp_path / "a.html").write_text("<html>\n</html>\n", encoding="utf-8")
    pending = PendingChanges(tmp_path)
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path, pending))
    registry.register(write_file_spec(tmp_path, pending))

    def verify(changed: list[str]) -> ToolResult:
        return ToolResult(ok=True, content="通过")

    verdict = json.dumps(
        {"action": "finish", "reason": "做完了",
         "message": "8 个目录逐个跑 pytest 都通过，任务已完成。"},
        ensure_ascii=False,
    )
    # 脚本顺序就是真实调用顺序：改完那一步之后步数预算见底（max_steps=1），
    # 循环在**下一步的步首**叫督导——所以督导的回答是脚本里的**第二条**。
    # finish 再把同一轮交给模型写 final，那是**第三条**。
    # 顺序写反的话，督导那次调用会拿不到脚本（fake 会抛「脚本已耗尽」）。
    loop = AgentLoop(
        gateway=FakeModel(
            script=[
                _turn("改", [{"name": "write_file",
                             "arguments": {"path": "a.html",
                                           "content": "<html>\n<body>\n</html>\n"}}]),
                verdict,
                _turn("写完", final="8 个目录逐个跑 pytest 都通过，任务已完成。"),
            ],
            tokenizer=OfflineTokenCounter(),
        ),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(
            project_root=tmp_path, context_window=4096, max_steps=1, supervise=True
        ),
        verify=verify,
        pending=pending,
    )
    result = loop.run("做点什么")
    assert result.finished is True
    assert result.final.startswith("8 个目录逐个跑 pytest 都通过，任务已完成。")
    assert "本次运行共落成 1 处改动" in result.final
    assert "指纹" in result.final


def test_督导说stop时结论也带真实状态(tmp_path: Path) -> None:
    """**用户那次的时序**：判词先出、落盘在后（或反之），结论必须说清改了没有。

    变异：去掉收尾那处 `_with_final_state`，这条会红——判词就会把已经落成的
    一处改动说成没发生（用户看到的正是那句「0 处改动」）。
    """
    (tmp_path / "a.html").write_text("<html>\n</html>\n", encoding="utf-8")
    pending = PendingChanges(tmp_path)
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path, pending))
    registry.register(write_file_spec(tmp_path, pending))
    verdict = json.dumps(
        {"action": "stop", "reason": "自动验证连续失败（0 处改动），说明它没有实际修改文件"},
        ensure_ascii=False,
    )
    loop = AgentLoop(
        gateway=FakeModel(
            script=[
                _turn("改", [{"name": "write_file",
                             "arguments": {"path": "a.html",
                                           "content": "<html>\n<body>\n</html>\n"}}]),
                verdict,
            ],
            tokenizer=OfflineTokenCounter(),
        ),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(
            project_root=tmp_path, context_window=4096, max_steps=1, supervise=True
        ),
        verify=lambda changed: ToolResult(ok=False, content="标签配对：未通过"),
        pending=pending,
    )
    result = loop.run("做点什么")
    assert result.finished is False
    # 这是真实运行记录：收手判词挂在后面，但**它说的那件事被记录驳倒了**。
    assert "本次运行共落成 1 处改动" in result.final
    assert "最终验证：未通过" in result.final
    assert "a.html：3 行" in result.final


# ---- E：意图的口径（提示词一侧的锚点核对在 test_prompt_mechanics.py）-------


def test_意图口径写进失败话术(tmp_path: Path) -> None:
    """拦下重提时必须把「想拿什么 + 接下来怎么做」这个口径说出来。

    意图取自它自己写的 why（这里走 `thought` 的退路：只有一个调用时会退到
    它）。本轮把硬拦前移到**第一次重复**，所以被回显的是第二次那句「再读」；
    口径仍然要一字不差地出现——那是模型照做的那部分。
    """
    (tmp_path / "a.txt").write_text("内容\n", encoding="utf-8")
    loop = _build(
        tmp_path,
        [_turn("读", [_read("a.txt")]), _turn("再读", [_read("a.txt")]),
         _turn("再看一遍文件", [_read("a.txt")]), _turn("算了", final="结束")],
    )
    loop.run("看看 a.txt")
    prompt = _last_prompt(loop)
    assert "你写的意图是「再读」" in prompt
    assert "口径：想拿到什么 + 拿到之后接下来怎么做" in prompt
    assert "拿到之后做什么" in prompt


def test_意图进事件流的字段没变() -> None:
    """`--events` 里 intent 仍是调用意图的出口（口径改了，字段不能改）。"""
    from spoolkit.tools.types import ToolCall

    call = ToolCall("read_file", {"path": "a.txt"}, intent="读 a.txt 确认要改哪一段")
    assert call.intent == "读 a.txt 确认要改哪一段"
    assert call_signature(call).startswith("read_file ")
