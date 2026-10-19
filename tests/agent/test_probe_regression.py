"""从真实探针跑分里挖出来的回归场景。

这些用例直接对应 `scripts/probe.py` 在真实模型下暴露的失败轨迹：
不是模型能力问题，而是主循环在「护栏放行」「上下文压缩」「督导改道」
交界处的行为把任务拖进死胡同。把它们钉成单元测试，复跑成本才可控。
"""

import json
from pathlib import Path

import pytest

from spoolkit.agent.loop import AgentLoop
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.fs import read_file_spec
from spoolkit.tools.registry import ToolRegistry


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


def _read(path: str) -> dict:
    return {"name": "read_file", "arguments": {"path": path}}


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
            max_steps=16,
            supervise=False,
            **kwargs,
        ),
    )


def _patch_bump_revision(monkeypatch, loop: AgentLoop) -> None:
    """每次调用前把上下文版本推一格，模拟真实运行里的压缩/重置。

    注意：循环在 `_invoke_guarded` 返回之后才会把 `_seen_at_revision`
    更新成当前版本号。如果我们在返回后再加，seen 会永远等于最新版本，
    刷新判据就永远不成立。真实时序是「下一次调用之前压缩已经发生」。
    """
    real = AgentLoop._invoke_guarded

    def wrapped(self, call, repeats, seen=1):
        self._context_revision += 1
        return real(self, call, repeats, seen)

    monkeypatch.setattr(AgentLoop, "_invoke_guarded", wrapped)


def test_读取非法语法_py_文件不会炸循环(tmp_path: Path) -> None:
    """真机现场：工作区里可能混着非代码的 .py 文件（测试夹具、失败摘要）。

    `_functions` 原来直接调 `ast.parse`，一遇到 SyntaxError 就把整个 AgentLoop
    炸掉。这里只要求它平静地跳过这段，让任务继续跑。
    """
    (tmp_path / "services.py").write_text(
        "AssertionError: assert service.stopped is True\nvalue = 1\n",
        encoding="utf-8",
    )
    loop = _build(
        tmp_path,
        [
            _turn("读失败摘要", [_read("services.py")]),
            _turn("收尾", final="这段不是代码，跳过即可。"),
        ],
    )
    result = loop.run("看看失败摘要")
    assert result.finished is True
    assert "跳过即可" in result.final


def test_查询类工具重复不被误判成判不出(tmp_path: Path) -> None:
    """rename-cross-file 真机现场：find_callers 第一次成功，第二次原样重发
    就被「没有可比较的内容对象」硬拦，第三次直接停用组合。

    查询类工具（find_callers / search_code 等）的可比对象不是文件内容，
    而是「参数 + 待确认清单」。参数没变、清单没变时重跑才是无用重复；
    清单变了（比如模型已经提出了改名），重跑就有理由。
    """
    (tmp_path / "pipeline.py").write_text(
        "def get_rows(source):\n    pass\n\ndef render(source):\n    return get_rows(source)\n",
        encoding="utf-8",
    )
    script = [
        _turn("查引用", [{"name": "find_callers", "arguments": {"name": "get_rows", "depth": 2}}]),
        _turn("再查一次", [{"name": "find_callers", "arguments": {"name": "get_rows", "depth": 2}}]),
        _turn("收尾", final="查完了"),
    ]
    registry = ToolRegistry()
    from spoolkit.index.tools import find_callers_spec
    registry.register(find_callers_spec(tmp_path / ":memory:", root=tmp_path))
    loop = AgentLoop(
        gateway=FakeModel(script=script, tokenizer=OfflineTokenCounter()),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(
            project_root=tmp_path,
            context_window=4096,
            max_steps=8,
            supervise=False,
        ),
    )
    result = loop.run("看看 get_rows 被谁用")
    # 第一次执行成功；第二次因为参数与清单都没变，应判成**无用重复**硬拦，
    # 而不是「判不出」。无用重复与判不出的处置都是拒绝，但理由字段不同。
    assert any("完全相同" in line or "无用重复" in line for line in result.trace), (
        f"第二次 find_callers 应判成无用重复，trace：{result.trace}"
    )
    assert not any("没有可比较的内容对象" in line for line in result.trace), (
        f"查询类工具不应再被判不出，trace：{result.trace}"
    )


def test_confirm_rewrite_接受_write_file_的_acked_disappeared_别名(tmp_path: Path) -> None:
    """merge-html 真机现场：模型把 write_file 的参数名 acked_disappeared
    原样搬进 confirm_rewrite，被参数校验直接打回，确认回合卡死。

    confirm_rewrite 的正式参数名是 `acked`，但应该兼容别名——模型看不清
    两个工具参数名的细微差别，系统不能因此把任务卡住。
    """
    from spoolkit.tools.edit import (
        PendingChanges,
        confirm_rewrite_spec,
        write_file_spec,
    )

    old = "".join(f'<p id="p{index}">第 {index} 行</p>\n' for index in range(12))
    new = "<html>\n<p id=\"p0\">第 0 行</p>\n</html>\n"
    target = tmp_path / "snake.html"
    target.write_text(old, encoding="utf-8")
    pending = PendingChanges(tmp_path)
    staged = write_file_spec(tmp_path, pending).handler(
        {"path": "snake.html", "content": new, "rewrite_reason": "合并重复段落"}
    )
    assert staged.ok, staged.content[:200]
    record = pending.staged_items()[0]
    token = record.token

    # 用 write_file 的参数名 acked_disappeared 来确认——这是真机现场。
    spec = confirm_rewrite_spec(tmp_path, pending)
    result = spec.handler(
        {"token": token, "acked_disappeared": [item.token for item in record.items]}
    )
    assert result.ok, result.content[:300]
    assert pending.staged_items() == []
    assert [one.path for one in pending.items()] == ["snake.html"]


def test_假完成_接下来将进行_被退回(tmp_path: Path) -> None:
    """rename-cross-file 真机现场：模型第 5 步说「load_rows 不存在，可以安全
    地重命名。接下来将进行重命名操作。」——一个文件没改就 done=true，
    terminated_by 直接收了「模型完成」。

    结论里出现「接下来/将/准备」却没有任何落盘证据时，必须把 done 退回，
    让它继续动手。改过东西再说「接下来」不算假完成（可能只是说明下一步）。
    """
    (tmp_path / "pipeline.py").write_text(
        "def get_rows(source):\n    pass\n",
        encoding="utf-8",
    )
    script = [
        _turn("查过了，load_rows 不存在，可以安全地重命名", final="接下来将进行重命名操作。"),
        # 被退回后模型改口直接收尾（这里只关心第一步的拦截）
        _turn("收尾", final="已确认 get_rows 无外部引用。"),
    ]
    loop = _build(tmp_path, script)
    result = loop.run("把 get_rows 改名成 load_rows")
    # 第一次 done 必须被退回，trace 里要留下「结论没有落盘」的记录。
    assert any("结论没有落盘" in line for line in result.trace), (
        f"假完成应被退回，trace：{result.trace}"
    )
    # 第二次 done 时模型没再说「接下来」，应该放行。
    assert result.finished is True
    assert "已确认" in result.final


def test_长文终稿里的接下来不要求落盘(tmp_path: Path) -> None:
    loop = _build(tmp_path, [])
    loop._goal_text = "写一份完整方案。"
    assert loop._bounce_unwritten("接下来我们看架构。下一步是部署。", ()) == ""


def test_编码任务说接下来仍要求落盘(tmp_path: Path) -> None:
    loop = _build(tmp_path, [])
    loop._goal_text = "把 get_rows 改名成 load_rows"
    bounced = loop._bounce_unwritten("接下来将进行重命名操作。", ())
    assert "一个文件都还没改" in bounced


def test_文档线索优先于实现() -> None:
    from spoolkit.agent.loop import _is_coding_goal

    assert _is_coding_goal("AI 社区平台实现方案") is False
    assert _is_coding_goal("修复 loop.py 的预算") is True


def test_长文终稿宣称改了文件不退回(tmp_path: Path) -> None:
    loop = _build(tmp_path, [])
    text = "已完成 docs/design.md 的结构说明。"
    assert loop._bounce_unwritten(text, (), require_landing=False) == ""


def test_长文终稿里的长代码块不退回(tmp_path: Path) -> None:
    loop = _build(tmp_path, [])
    body = "```python\n" + "\n".join(f"x{i} = {i}" for i in range(25)) + "\n```"
    assert loop._bounce_unwritten(body, (), require_landing=False) == ""


def test_编码任务同文本仍要求落盘(tmp_path: Path) -> None:
    loop = _build(tmp_path, [])
    text = "已完成 docs/design.md 的结构说明。"
    bounced = loop._bounce_unwritten(text, (), require_landing=True)
    assert "design.md" in bounced
    code = "```python\n" + "\n".join(f"x{i} = {i}" for i in range(25)) + "\n```"
    assert "不会落盘" in loop._bounce_unwritten(code, (), require_landing=True)


def test_谎称完成_宣称改了没改的文件_被退回(tmp_path: Path) -> None:
    """rename-cross-file 第二轮真机现场：模型只改了 fetch.py，final 却说
    「pipeline/report.py 文件中的 render 函数已更新为 load_rows。请检查并
    确认改动。」——report.py 一个字没动，false_completion 记下 1。

    谎称完成比「接下来将进行」更狠：它用过去式，听上去像已经做完了。
    必须把 final 里宣称改了但 changed_paths 里没有的文件名抓出来退回。
    """
    (tmp_path / "pipeline").mkdir()
    (tmp_path / "pipeline" / "fetch.py").write_text(
        "def load_rows(source):\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "pipeline" / "report.py").write_text(
        "from pipeline.fetch import get_rows\n",
        encoding="utf-8",
    )
    script = [
        # 第一步：模型改了 fetch.py（模拟已经落盘）
        _turn("改 fetch.py", [{"name": "write_file", "arguments": {
            "path": "pipeline/fetch.py",
            "content": "def load_rows(source):\n    pass\n",
            "rewrite_reason": "重命名",
        }}]),
        # 第二步：模型谎称改了 report.py
        _turn("收尾", final="pipeline/report.py 文件中的 render 函数已更新为 load_rows。请检查并确认改动。"),
        # 被退回后模型改口说实话
        _turn("收尾", final="已把 fetch.py 里的 get_rows 改名为 load_rows。"),
    ]
    loop = _build(tmp_path, script)
    result = loop.run("把 get_rows 改名成 load_rows")
    # 谎称完成必须被退回，trace 里要留下「结论没有落盘」的记录。
    assert any("结论没有落盘" in line for line in result.trace), (
        f"谎称完成应被退回，trace：{result.trace}"
    )
    assert result.finished is True


def test_组合停用上报督导按二的幂退避(tmp_path: Path) -> None:
    """fix-bug 真机现场：read_file 同一组合被拦 9 次，每一次都把督导叫起来，
    督导 8 次给出逐字相同的建议——每次督导调用都真花钱，模型对同一句话
    也早已免疫。同一组合的停用上报必须按 2 的幂退避（第 1、2、4、8… 次
    才惊动督导），中间的拦截只走机械话术。
    """
    from spoolkit.agent.protocol import ToolCall

    loop = _build(tmp_path, [])
    call = ToolCall(name="read_file", arguments={"path": "a.txt"}, intent="读")
    signature = "sig-1"
    # 同一组合连拦 8 次：第 1 次还是普通拦截（count <= block_at），
    # 第 2 次起组合停用；停用证据只在第 2、4、8 次（2 的幂）留给督导。
    evidence_set_at: list[int] = []
    for count in range(1, 9):
        loop._reroute_evidence = ""
        loop._refuse(call, signature, "被拒", ())
        if loop._reroute_evidence:
            evidence_set_at.append(count)
    assert evidence_set_at == [2, 4, 8], (
        f"停用上报应在 2/4/8 次时留给督导，实际：{evidence_set_at}"
    )


def test_刷新读取到上限后硬拦不再执行(tmp_path: Path, monkeypatch) -> None:
    """真实轨迹：模型反复读同一个文件，因为每次结果都被上下文压缩丢弃。

    护栏允许「结果被压掉」这种重取**两次**（D6：这条账单独设份额，见
    `loop._REFRESH_COMPACTED_LIMIT`），第 3 次重复起必须硬拦——否则它会一直把
    预算烧到收敛/步数上限，根本没机会写文件。

    为什么不是原来的 3 次：那 3 次是**一个池子**，而烧它的成因不是同一种。
    实测形状是全给「结果被压掉」吃光，等文件真的变了、模型必须拿到新正文时，
    账上空了 → 硬拦，而拦下的话术还说「继续重跑不会再拿到新内容」——那句在
    那一刻是假的。分账之后总额度**一分不加**（全局仍是 `_REFRESH_LIMIT`=3），
    只是「压掉」这一本更小，留一次给"重取真能拿到新内容"那一种
    （`test_压缩账用完后内容真变了仍放行` 钉的就是那一次）。

    变异：把「额度用满后照常执行」那一条改回去，这条会红——
    实际执行次数会超过 3 次，而且 blocked_refund 不会触发。
    """
    (tmp_path / "a.txt").write_text("内容\n" * 50, encoding="utf-8")
    loop = _build(
        tmp_path,
        [
            _turn("读", [_read("a.txt")]),
            _turn("再读", [_read("a.txt")]),
            _turn("还读", [_read("a.txt")]),
            _turn("又读", [_read("a.txt")]),
            _turn("继续读", [_read("a.txt")]),
            _turn("还在读", [_read("a.txt")]),
            _turn("收尾", final="不玩了"),
        ],
    )
    _patch_bump_revision(monkeypatch, loop)

    executed: list[str] = []
    real_invoke = loop.registry.invoke

    def counting(call):
        executed.append(call.name)
        return real_invoke(call)

    loop.registry.invoke = counting

    result = loop.run("看看 a.txt")
    # 初始 1 次 + 2 次放行刷新（「结果被压掉」那本账的份额）= 3 次真正执行；
    # 第 3 次重复（总第 4 次调用）起硬拦。
    assert executed.count("read_file") == 3, (
        f"刷新额度用满后仍在执行：read_file 实际执行 {executed.count('read_file')} 次"
    )
    # 账目本身要读得出来：压掉这一本用满 2/2，**总账还剩 1 次**（留给新内容）。
    assert loop._refreshes_by_cause == {"compacted": 2}, loop._refreshes_by_cause
    assert loop._refreshes_used == 2, loop._refreshes_used
    # 硬拦的步数必须退还，不占用有效预算。
    assert any("这一步的调用全部被硬拦" in line for line in result.trace), result.trace
    assert any("已退还" in line for line in result.trace), result.trace
    # 拒绝话术必须说清是哪一本账空了，并且**说真话**：内容真变了还有一次机会
    # （旧话术一律写成「额度已用完」，而总账其实还剩一次）。
    prompt = "\n".join(message.content for message in loop.gateway.requests[-1].messages)
    assert "结果被上下文压掉」这本账已经用完" in prompt, prompt
    assert "确实变了" in prompt, prompt
    assert "总额度已经用完" not in prompt, prompt


def test_压缩账用完后内容真变了仍放行(tmp_path: Path, monkeypatch) -> None:
    """分账的那一次预留真的能用上（D6 的**行为**判据，不是读数判据）。

    轨迹里刻意**先撞一次「这本账用完了」**再改文件，否则这条用例测不出预留：

        读① 初始 → 读② 压掉 1/2 → 读③ 压掉 2/2 → 读④ 压掉（**这本用完，硬拦**）
        → 文件真的变了 → 读⑤ 新内容（用掉预留的**第 3 次**，放行）
        → 读⑥ 总额度用完（硬拦）

    为什么非要多那一次「读④」：把按成因的份额整个去掉（或只查总数）时，读④
    会照常放行成"压掉 3/3"，于是读⑤（内容变了）撞上总数上限被硬拦。少了它，
    读⑤ 是被**全局上限**放行的，与预留无关——那样这条用例对预留就是空转的
    （三种变异下全绿）。

    所以断言盯的是账目本身（`by_cause`）与那条成因标签，而不只是执行次数；
    总额度也不许被分账偷偷放大（仍 ≤ `_REFRESH_LIMIT`）。
    """
    target = tmp_path / "a.txt"
    target.write_text("第一版\n", encoding="utf-8")
    loop = _build(
        tmp_path,
        [
            _turn("读", [_read("a.txt")]),
            _turn("再读", [_read("a.txt")]),
            _turn("还读", [_read("a.txt")]),      # 第 2 次重复 → 压掉账满
            _turn("又读", [_read("a.txt")]),      # 第 3 次重复 → 这本账用完，硬拦
            _turn("它变了吧", [_read("a.txt")]),   # 文件已改 → 走预留的那一次
            _turn("再来一遍", [_read("a.txt")]),   # 总额度用完 → 必须硬拦
            _turn("收尾", final="看完了"),
        ],
    )
    real_guard = AgentLoop._invoke_guarded
    attempts: list[str] = []

    def wrapped(self, call, repeats, seen=1):
        # 每次调用之前：模拟压缩（版本推进 → 原结果不在手上）。
        self._context_revision += 1
        # 第 5 次调用之前把文件改掉：真实轨迹里正是模型自己写完了再回头读。
        # 判据在 `_invoke_guarded` 里算，所以改动必须发生在这之前。
        if len(attempts) == 4:
            target.write_text("第二版\n", encoding="utf-8")
        attempts.append(call.name)
        return real_guard(self, call, repeats, seen)

    monkeypatch.setattr(AgentLoop, "_invoke_guarded", wrapped)

    executed: list[str] = []
    real_invoke = loop.registry.invoke

    def counting(call):
        executed.append(call.name)
        return real_invoke(call)

    loop.registry.invoke = counting

    result = loop.run("看看 a.txt")

    # 初始 1 + 压掉账 2 + 新内容账 1 = 4 次真正执行（= 1 + 全局上限 3，没放宽）。
    assert executed.count("read_file") == 4, (
        f"新内容那一次预留没被放行：{executed.count('read_file')} 次"
    )
    assert loop._refreshes_by_cause == {"compacted": 2, "changed": 1}, (
        loop._refreshes_by_cause
    )
    assert loop._refreshes_used == 3, loop._refreshes_used
    # 读④ 那次硬拦必须点名「这本账」（不是总额度）——它正是预留的来源。
    assert any("这本账已经用完" in line for line in result.trace), result.trace
    # 读⑤ 那次放行的回执必须点名它记在哪本账上（模型与 trace 都读得到）。
    assert any("要看的东西真的变了" in line for line in result.trace), result.trace
    # 上限仍被守住：预留用完之后，内容再变也没有额度了；而且话术**不许**再
    # 承诺「先做一处改动再回来看」（那句在总额度用完时兑现不了）。
    assert any("总额度已经用完" in line for line in result.trace), result.trace
    assert not any("先做一处真正的改动再回来看" in line for line in result.trace), (
        result.trace
    )
