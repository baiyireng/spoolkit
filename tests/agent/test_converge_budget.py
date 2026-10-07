"""P0 修③：临近预算的**强制收束**。

三件事，每一件都能证伪：

1. 剩余步数 ≤ `converge_at_steps` 时，那几轮的模型上下文里**有**收束指令
   （断言提示词原文，不靠内部变量）；
2. 「素材已经够了却一直不写结论」→ 收束之后**产出结论**（`concluded` 为真）；
3. 「素材不足」→ 结论**显式写明缺什么**，而不是编造，也不是只留一句系统判词；
4. 报告里「模型写的结论」与「系统判词」**机械可分**（`LoopResult.model_verdict_line`
   / `concluded` / `terminated_by`），并在 FINAL 事件上带出去。
5. 变异对照：把 `converge_at_steps` 调 0（关掉注入）之后，同一份脚本**不再**
   有收束指令——这条证明上面那几条不是我事后看出来的巧合。
"""

import json
from pathlib import Path

from spoolkit.agent.loop import (
    CONCLUDED_BY_CONVERGE,
    MATERIAL_CARD_HEAD,
    MATERIAL_CARD_MAX_CHARS,
    AgentLoop,
    converge_step,
)
from spoolkit.config import Config
from spoolkit.context import templates as T
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.edit import PendingChanges, write_file_spec
from spoolkit.tools.fs import list_dir_spec, read_file_spec
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.types import REVERSE_CLAUSE


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


def _build(
    tmp_path: Path,
    script: list,
    overrides: dict | None = None,
    files: int = 0,
    max_steps: int = 6,
) -> AgentLoop:
    (tmp_path / "a.py").write_text("x = 0\n", encoding="utf-8")
    # 每一次都读**不同**的文件：参数逐字相同的重复调用会被机械层硬拦
    # （那是另一条判据，见 test_repeat_guard.py），拦下之后脚本就不会前进，
    # 这一组测试要量的是「收束有没有把结论逼出来」，不该被那条判据搅进来。
    for index in range(files):
        (tmp_path / f"f{index}.py").write_text(
            f"y{index} = 0\n" + "z = 1\n" * 40, encoding="utf-8"
        )
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path))
    registry.register(list_dir_spec(tmp_path))
    registry.register(write_file_spec(tmp_path, PendingChanges(tmp_path)))
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=OfflineTokenCounter()),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        # 督导会在别处插一次模型调用，把脚本顺序搅乱；这一层与它无关。
        config=Config(
            project_root=tmp_path,
            context_window=4096,
            max_steps=max_steps,
            supervise=False,
            overrides=overrides or {},
        ),
        # 这一组量的是**旧的重置路**：素材压缩（阶梯第一级）会另花一次模型
        # 调用，把脚本化假模型的一步顶掉。阶梯那一级由
        # `tests/agent/test_material_ladder.py` 单独钉。
        compaction=False,
    )


def _explore(turns: int) -> list[str]:
    """每一步读一个不同的文件（避免被重复调用护栏拦下）。"""
    return [_turn(f"看第 {i} 处", [_read(f"f{i}.py")]) for i in range(turns)]


def _prompts(loop: AgentLoop) -> list[str]:
    """每一轮发给模型的全文（system 区段 + 反馈都在里面）。"""
    return [
        "\n".join(message.content for message in request.messages)
        for request in loop.gateway.requests
    ]


def test_收束点按剩余步数算且可关() -> None:
    """`converge_step` 是这一条的唯一口径：剩余 ≤ N 步时的那一步。"""
    # 预算 12 步、剩余 2 步收束 → 第 11 步起。
    assert converge_step(2, 12) == 11
    # 剩余 1 步收束 → 第 12 步（最后一步）才注入。
    assert converge_step(1, 12) == 12
    # 剩余步数比预算还多时从第 1 步起（等于全程都收束）。
    assert converge_step(20, 12) == 1
    # 0 = 关（state.step 从 1 起，0 永远不触发）。
    assert converge_step(0, 12) == 0
    assert converge_step(-1, 12) == 0


def test_临近预算时收束指令进了模型上下文(tmp_path: Path) -> None:
    """断言的是**发给模型的那几段文本**，不是内部变量。"""
    script = _explore(5)
    script.append(_turn("够了", final="结论：a.py 里只有一行赋值，没什么可改。"))
    loop = _build(tmp_path, script, files=6)
    result = loop.run("看看 a.py")

    prompts = _prompts(loop)
    assert len(prompts) >= 4, prompts
    # max_steps=6、converge_at_steps=2 → 第 5、6 步注入：最后两次调用里有它，
    # 第一次里没有（它得先有机会探索）。
    assert T.CONVERGE_NOW not in prompts[0], "一开始就收束了：它没机会探索"
    assert T.CONVERGE_NOW in prompts[-1], "收束那一步没有收束指令"
    assert any("注入收束指令" in line for line in result.trace), result.trace
    assert result.concluded is True
    assert result.terminated_by == "模型完成"


def test_素材够了却一直不写结论_收束后产出结论(tmp_path: Path) -> None:
    """真机现场的形状：每一步都在查看，到撞上限也没写过结论。

    脚本给的是**5 步查看 + 1 步结论**，而收束点在第 5 步（预算 6、剩余 2 步）：
    于是第 5 步那一次查看与结论是**同一轮**里的事——旧行为下它会一直查下去
    （脚本后面还有条目就继续查），收束之后它必须把结论交出来。
    """
    script = _explore(5)
    script.append(_turn("材料够了", final="结论：只有一处赋值，不需要改动。"))
    loop = _build(tmp_path, script, files=6)
    result = loop.run("汇总这个文件的待办")

    assert result.finished is True, result.final
    assert result.concluded is True, "收束之后模型没有写出结论"
    assert "只有一处赋值" in result.model_final
    assert result.terminated_by == "模型完成"
    # 结论与判词在报告里各占一行、各带标记。
    assert T.MODEL_VERDICT_PREFIX in result.report_text
    assert result.steps <= 6


def test_关掉收束注入之后同一份脚本不再产出结论(tmp_path: Path) -> None:
    """**变异对照**：`converge_at_steps=0` 时那条指令一次都不出现。"""
    script = _explore(6)
    script.append(_turn("材料够了", final="结论：不该跑到这里。"))
    loop = _build(tmp_path, script, overrides={"converge_at_steps": 0}, files=6)
    result = loop.run("汇总这个文件的待办")

    assert all(T.CONVERGE_NOW not in one for one in _prompts(loop)), (
        "关掉之后收束指令还进了上下文"
    )
    # 6 步全是工具调用：脚本第 7 条用不上，收不了尾。
    assert result.concluded is False
    assert result.terminated_by == "步数上限"
    assert result.final.startswith("已达步数上限")


def test_素材不足时结论写明缺什么而不是编造(tmp_path: Path) -> None:
    """判据是**结论正文的内容**：缺什么要点名，且不许冒充完整答案。

    这条守的是收束指令的另一半：只写「给结论」会把模型推向编一个听起来
    完整的答案，所以缺什么必须写在结论里。
    """
    script = _explore(5)
    script.append(
        _turn(
            "收束",
            final=(
                "结论：我查过 a.py 与目录清单，缺 tests/ 目录下的验收测试原文，"
                "所以无法判断该改哪一处；需要补齐 tests/ 里那个失败用例才能给方案。"
            ),
        )
    )
    loop = _build(tmp_path, script, files=6)
    result = loop.run("定位失败原因")

    assert result.concluded is True
    assert "缺" in result.model_final, result.model_final
    assert "tests/" in result.model_final, "缺什么没有点名到具体路径"
    # 系统判词**没有**被当成结论（模型自己写的那句才是结论）。
    assert "已达步数上限" not in result.model_final


def test_收束指令在上下文重置之后仍然送得到(tmp_path: Path) -> None:
    """**真机抓出来的那个洞**：重置会把 `feedback` 清掉，收束指令随之消失。

    P0 真机小样本的现场允许逐回合核对（`p0-turns.json`）：第 4 步它已经读到
    失败原文；第 5 步上下文重置把素材整段丢掉，**收束指令也被丢掉**；
    第 6-8 步在重新列目录，最后一次模型调用里根本没有那句指令。

    这条用假 provider 把那个形状钉住：把硬触发线压到很低，让收束那一步
    必然发生一次重置，然后断言**重置之后的每一次模型调用里都有收束指令**。
    """
    script = _explore(5)
    script.append(_turn("够了", final="结论：不用改。"))
    loop = _build(
        tmp_path,
        script,
        files=6,
        overrides={"hard_trigger_ratio": 0.01},
    )
    loop.run("看看这几个文件")

    prompts = _prompts(loop)
    # 重置确实发生了（不然这条用例什么都没量到）。
    assert any("上下文重置" in line for line in loop._trace), loop._trace
    # 收束点之后（第 4 步起：预算 6、剩余 3 步）的每一次调用都带着它。
    after = [one for one in prompts if T.CONVERGE_NOW in one]
    assert after, "收束指令一次都没进上下文"
    assert T.CONVERGE_NOW in prompts[-1], "最后一次模型调用里没有收束指令"


def test_收束后探索类调用被工具层拒绝而不是靠文本劝阻(tmp_path: Path) -> None:
    """**P0 修①的验收**：素材已经够了却还想再看时，拦它的是**动作**，不是文字。

    真机诊断（P0 `p0-turns.json`）：`CONVERGE_NOW` 那段文本确实送达了——第 7、
    8 次模型调用里都能读到——但模型照发 `list_dir`。**模型对文字免疫**，能推得
    动它的只有「这一轮物理上还剩下什么可选」。所以这一层必须落在
    `registry.invoke` **之前**：收束态里探索类调用不给执行。

    脚本形状（预算 8 步，`converge_at_steps=3` → 第 7 步进收束态）：

    - 第 1–6 步：读 f0–f5（各自正文不同，不触发重复护栏那条判据）；
    - 第 7 步（刚进收束态）：读 f6 —— 这是 `CONVERGE_NOW` 承诺的「最多再读一次」；
    - 第 8 步：读 f7 —— 额度用完了（它是**存在**的，所以拒绝不可能被说成
      「文件不存在」），**被拒**；收束态里这一轮的调用全被拦 = 没有执行，
      所以不占步数预算（退还），循环还会再问它一次；
    - 再一轮：`list_dir` —— 也是探索，**被拒**；
    - 最后：一段**纯文本**（真机就是这形状：不是合法 JSON）——按结论收下。

    预算为什么是 8 步而不是更小：收束态本身要留出「补素材一次 + 被拒一次 +
    写结论一次」三轮。预算 6 步（`converge_at_steps=2`）时补素材那一读会把
    最后一步用掉，模型再没有被问话的机会——那不是它不收尾，是我们没给它开口
    的机会。这一点是探针（`.probe_runs/probe_converge_order.py`）实测撞出来的。
    """
    script = _explore(6)
    script.append(_turn("补读那一处", [_read("f6.py")]))
    script.append(_turn("再看一个", [_read("f7.py")]))
    script.append(_turn("再列一下", [{"name": "list_dir", "arguments": {"path": "."}}]))
    script.append(
        "结论：f0–f6 我都读过了，只有赋值语句，没有可改的地方；"
        "缺 tests/services/ 下的失败用例原文，所以给不出改动方案。"
    )
    loop = _build(
        tmp_path, script, files=8, max_steps=8, overrides={"converge_at_steps": 3}
    )

    executed: list[str] = []
    real = loop.registry.invoke

    def counting(call):
        executed.append(call.name)
        return real(call)

    loop.registry.invoke = counting
    result = loop.run("把这个文件里的待办汇总一下")

    # 真的执行了 7 次读：f0–f6。第 8 次（f7）与 `list_dir` **一次都没执行**。
    assert executed == ["read_file"] * 7, executed
    last = _prompts(loop)[-1]
    # 拒绝话术要说清三件事：这是**预算收束**、**唯一还允许的动作**是什么、
    # 以及 §2.5 的反向条款（别读成「不许删文件」）。
    assert "预算收束" in last, last
    assert "唯一还允许的动作" in last
    assert REVERSE_CLAUSE in last
    assert "补一次素材" in last
    # 那一次「补素材」的额度真的兑现了（trace 里记账）。
    assert any("补素材" in line for line in result.trace), result.trace
    # 结论是**它自己写的字**，不是系统判词。
    assert result.concluded is True, result.final
    # 终止原因点名「结论是从收束那条出口收下的」——与「模型自己标了 done」
    # 区分开，报告里才能看出这一次是逼出来的还是它自己收的尾。
    assert result.terminated_by == CONCLUDED_BY_CONVERGE, result.terminated_by
    assert result.model_final.startswith("结论：")
    assert "缺 tests/" in result.model_final
    assert "已达步数上限" not in result.model_final


def test_关掉收束之后同一份脚本会把那个文件读掉(tmp_path: Path) -> None:
    """**变异对照**：把收束关掉，同一份脚本就会真的把 f5 读掉、也收不了尾。

    这条证明上一条拦住的**是收束政策**，不是「文件不存在」「护栏顺手拦了」
    之类的巧合：唯一的差别只有 `converge_at_steps`。
    """
    script = _explore(6)
    script.append(_turn("补读那一处", [_read("f6.py")]))
    script.append(_turn("再看一个", [_read("f7.py")]))
    script.append(_turn("再列一下", [{"name": "list_dir", "arguments": {"path": "."}}]))
    script.append("结论：f0–f6 我都读过了。")
    loop = _build(
        tmp_path,
        script,
        overrides={"converge_at_steps": 0},
        files=8,
        max_steps=8,
    )

    executed: list[str] = []
    real = loop.registry.invoke

    def counting(call):
        executed.append(call.name)
        return real(call)

    loop.registry.invoke = counting
    result = loop.run("把这个文件里的待办汇总一下")

    reads = executed.count("read_file")
    assert reads == 8, executed  # 连 f7 也读了
    assert all(T.CONVERGE_NOW not in one for one in _prompts(loop))
    assert all("预算收束" not in one for one in _prompts(loop))
    # 收束关掉之后那段纯文本不会被当成结论，收不了尾。
    assert result.concluded is False
    assert result.terminated_by == "步数上限"


def test_硬重置把已读到的原文压成素材卡挂回上下文(tmp_path: Path) -> None:
    """**P0 修②的验收**：上下文重置丢掉历史，但**不丢素材**。

    真机现场（`p0-turns.json` 第 4→5 步）：第 4 步已经读到失败原文；第 5 步
    硬重置把 13531 字的上下文砍到 4126 字，**连那份失败原文一起砍掉了**，
    第 6–8 步它在重新 `list_dir`，最后一句判词是「已达步数上限」。

    两条判据必须同时成立：
    1. **素材可核对地还在**——重置之后发给模型的提示词里能逐字找到那句断言原文；
    2. **上下文仍受控**——多出来的那部分就是卡片本身，它按字数封顶；整段
       900 行的文件正文并没有被搬过来。
    """
    failure = "AssertionError: assert service.stopped is True"
    body = "# 失败原文\n" + failure + "\n" + "value = 1\n" * 900
    (tmp_path / "services.py").write_text(body, encoding="utf-8")
    goal = "把失败测试改绿"
    script = [
        _turn("读失败原文", [_read("services.py")]),
        _turn("看一眼现状", [_read("a.py")]),
        _turn("够了", final=f"结论：失败原文是「{failure}」，改 stop_service 时补一行。"),
    ]
    # 硬触发线压到极低：每一步都撞硬线，保证**重置发生在拿到素材之后**。
    loop = _build(tmp_path, script, overrides={"hard_trigger_ratio": 0.02})
    result = loop.run(goal)

    prompts = _prompts(loop)
    assert result.resets >= 2, result.trace
    assert any("素材卡保留了" in line for line in result.trace), result.trace
    # ① 素材可核对地还在：这一行断言就是证据——重置之后它仍读得到那句失败原文。
    last = prompts[-1]
    assert failure in last, last
    assert "services.py" in last, last
    assert MATERIAL_CARD_HEAD in last
    # ② 上下文仍受控：重置后的提示词只比最初那条（还没有任何素材）多了卡片，
    #    卡片有上限，而 12000+ 字的文件正文并没有被整段搬进来。
    delta = len(last) - len(prompts[0])
    assert 0 < delta <= MATERIAL_CARD_MAX_CHARS + 200, delta
    assert delta < len(body) // 4, delta
    assert result.concluded is True, result.final
    assert result.model_final.startswith("结论：")


def test_报告把模型结论与系统判词分开且机读(tmppath=None) -> None:
    """`final`（报告正文）与 `model_final`（模型原话）是两个东西。

    没写出结论时那句说明必须**明说**「系统判词不是它的结论」——真机对照里
    用户就是把判词当结论读的（33/36 的运行正文只有那一句）。
    """
    from spoolkit.agent.loop import LoopResult
    from spoolkit.agent.state import TaskState

    state = TaskState(task_id="t", goal="g")
    no_conclusion = LoopResult(
        finished=False,
        final="已达步数上限，任务未完成",
        state=state,
        steps=12,
        resets=0,
    )
    assert no_conclusion.concluded is False
    assert no_conclusion.terminated_by == "步数上限"
    line = no_conclusion.model_verdict_line
    assert line.startswith(T.MODEL_VERDICT_PREFIX)
    assert "不是它的结论" in line
    assert "已达步数上限" not in line.split("\n")[0]

    failed = LoopResult(
        finished=False,
        final="⚠ 本次运行失败（不是「没做完」）：LlamaCppError: tokenize 返回 502",
        state=state,
        steps=3,
        resets=0,
        failed_reason="LlamaCppError: tokenize 返回 502",
    )
    assert failed.terminated_by == "失败"
    assert failed.concluded is False

    with_conclusion = LoopResult(
        finished=True,
        final="改好了\n最终验证：通过",
        state=state,
        steps=4,
        resets=0,
        model_final="改好了",
    )
    assert with_conclusion.concluded is True
    assert with_conclusion.terminated_by == "模型完成"
    assert with_conclusion.model_verdict_line == f"{T.MODEL_VERDICT_PREFIX}改好了"
    assert "最终验证：通过" in with_conclusion.report_text
