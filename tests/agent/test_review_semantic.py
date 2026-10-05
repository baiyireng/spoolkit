"""L3 语义审查的用例（设计稿 §5 L3 / §3.1 / §3.2.0 / §2.5 / §6 P2）。

这一层存在的唯一理由是抓「**看起来对、其实没做到**」——L1 是机械一致性、
L2 是行为回归，两者都答不出「它声称做到了，做到了吗」。首轮
`rename-cross-file` 就是这一类：结论写「整个项目里都统一了」，而 README 与
docs 里的旧名一个没动。

所以用例钉的是这条链上的六件事，每件都能证伪：

1. **结构化发现**：`file:line` / 严重度 / 证据三样齐全；指不出位置的、
   位置与问题都没有的，**不上报**（§4）。
2. **事实核对**：说「工具不存在」「零改动」「这处改动不在 diff 里」的断言，
   与运行记录核对后被**降级/改写**（复用 `agents/supervisor` 那一套——
   我们被督导幻觉坑过，L3 更不能例外）。
3. **独立上下文**：送审提示里**没有**实现者的自我陈述——这不是靠调用方
   记得少传一个参数，而是 `ReviewMaterials` 里根本没有那个字段。
4. **触发条件**：不该跑时不跑（一次模型调用都不发生），该跑时逐条给理由；
   发现条数有上限（超上限要如实说）。
5. **高严重度未处理 → 不许宣称完成**（§3.1 第 3 条）：走真循环，`l3_blocked`
   发出来、T9 命中、`final_ok=False`，而**成果照旧在盘上**（§2.5）。
6. **模型可见文本带 §2.5 反向条款**，且不得读成「禁止删除」。

用例全部走**假 provider**（注入 `chat`），**不真调模型**。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spoolkit.agent import review_semantic as RS
from spoolkit.agent.audit import HIGH, INFO, SEMANTIC_FINDING_KIND, Finding
from spoolkit.agent.loop import AgentLoop
from spoolkit.agent.review_gate import EscalationGate
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.llm.types import ChatRequest, ChatResponse
from spoolkit.tools.edit import PendingChanges, register_edit_tools
from spoolkit.tools.fs import list_dir_spec, read_file_spec
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.types import ToolResult


# ---- 小工具 ------------------------------------------------------------------


def materials(root: Path, **over) -> RS.ReviewMaterials:
    """一份最小的送审材料：两处改动 + 一段 diff。"""
    base = dict(
        goal="把所有调用点都改成 load_rows",
        changes=(
            RS.ChangeFacts(path="pipeline/fetch.py", edited=True, added=10, removed=10),
            RS.ChangeFacts(path="pipeline/report.py", edited=True, added=5, removed=5),
        ),
        diff=(
            "--- a/pipeline/fetch.py\n+++ b/pipeline/fetch.py\n"
            "-def get_rows():\n+def load_rows():\n"
        ),
        checks_run=("最终验证：通过——2 passed",),
        revert_command="spool revert --run r1",
    )
    base.update(over)
    return RS.ReviewMaterials(**base)


def payload(findings: list[dict], verdict: str = "fail", reasons=None) -> str:
    return json.dumps(
        {
            "findings": findings,
            "verdict": verdict,
            "reasons": reasons or ["还有残留"],
        },
        ensure_ascii=False,
    )


def run_review(root: Path, text: str, **over) -> RS.SemanticReport:
    """用假 provider 跑一次 L3（触发条件显式给上）。"""
    return RS.review(
        materials(root, **over),
        root=root,
        decision=RS.decide_triggers(finding_count=1),
        chat=lambda _prompt: text,
    )


def make_tree(root: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return root


# ---- ① 结构化发现（schema）---------------------------------------------------


def test_发现形状是文件行号严重度证据(tmp_path: Path) -> None:
    make_tree(tmp_path, {"pipeline/report.py": "x = 1\n"})
    report = run_review(
        tmp_path,
        payload(
            [
                {
                    "file": "pipeline/report.py",
                    "line": 12,
                    "problem": "还有一处调用点没改",
                    "severity": "high",
                    "suggestion": "一起改掉",
                    "evidence": "diff 里 report.py 仍是 get_rows",
                }
            ]
        ),
    )

    assert report.ran is True
    assert len(report.findings) == 1
    item = report.findings[0]
    assert item.file == "pipeline/report.py"
    assert item.line == 12
    assert item.severity == RS.SEVERITY_HIGH
    assert item.evidence == "diff 里 report.py 仍是 get_rows"
    assert item.where == "pipeline/report.py:12"
    assert item.blocking is True
    assert item.audit_severity == HIGH
    # 进事件流的形状带齐字段（探针与壳按这些键读，不读散文）。
    body = item.to_payload()
    for key in ("file", "line", "severity", "verified", "blocking", "problem", "evidence"):
        assert key in body, key


def test_严重度三档映射到账本两档且只有high拦完成() -> None:
    assert RS.SEVERITY_TO_AUDIT[RS.SEVERITY_HIGH] == HIGH
    assert RS.SEVERITY_TO_AUDIT[RS.SEVERITY_MEDIUM] == INFO
    assert RS.SEVERITY_TO_AUDIT[RS.SEVERITY_LOW] == INFO
    for severity in (RS.SEVERITY_MEDIUM, RS.SEVERITY_LOW):
        item = RS.SemanticFinding(
            file="a.py", line=1, problem="p", severity=severity, verified=True
        )
        assert item.blocking is False, severity
    # 没过核对的发现**一律不拦**（哪怕它自称 high）。
    item = RS.SemanticFinding(
        file="a.py", line=1, problem="p", severity=RS.SEVERITY_HIGH, verified=False
    )
    assert item.blocking is False
    assert item.audit_severity == INFO


def test_指不出位置的发现不上报(tmp_path: Path) -> None:
    """§4：每条发现必须带 `file:line` 与机械证据，否则不上报。"""
    report = run_review(
        tmp_path,
        payload(
            [
                {"file": "", "line": 0, "problem": "", "severity": "high"},
                {"file": "a.py", "line": 0, "problem": "这里有问题", "severity": "low"},
            ]
        ),
    )

    assert len(report.findings) == 1, [item.render() for item in report.findings]
    assert report.findings[0].line_known is False
    assert report.findings[0].where == "a.py"


def test_schema三档严重度与解析分支一致() -> None:
    enum = RS.REVIEW_SCHEMA["properties"]["findings"]["items"]["properties"]["severity"]
    assert set(enum["enum"]) == set(RS.AUDIT_SEVERITIES)
    assert set(RS.AUDIT_SEVERITIES) == set(RS.SEVERITY_TO_AUDIT)


def test_解析不出来按未通过但不据此阻断(tmp_path: Path) -> None:
    """「没结论」与「格式不对」分开，两者都**不据此拦完成声明**（§4 的误报）。"""
    for text, needle in (("这不是 JSON", "无法解析"), ('{"verdict":"pass"}', "没有 findings")):
        report = run_review(tmp_path, text)
        assert report.ran is True
        assert needle in report.cannot_check, report.cannot_check
        assert report.findings == ()
        assert report.blocked is False


# ---- ② 事实核对（被丢弃/改写）------------------------------------------------


def test_说工具不存在被核对掉(tmp_path: Path) -> None:
    """复用督导那一套：注册表里有的工具被说成「不存在」→ 降级 + 指出冲突。"""
    facts = RS.ReviewFacts(
        tools=frozenset({"confirm_rewrite", "read_file"}),
        edits=2,
        changed=frozenset({"pipeline/report.py"}),
    )
    finding = RS.SemanticFinding(
        file="pipeline/report.py",
        line=4,
        problem="这里调用不存在的工具 confirm_rewrite，所以没改完",
        severity=RS.SEVERITY_HIGH,
        evidence="工具不存在",
    )
    checked = RS.check_findings([finding], facts)

    assert checked.kept == ()
    assert checked.conflicts[RS.CONFLICT_TOOL_MISSING] == 1
    assert "confirm_rewrite" in checked.unverified[0].conflict_note()
    assert checked.unverified[0].blocking is False


def test_循环把注册表交给L3的事实核对(tmp_path: Path) -> None:
    """接线那一半：`ReviewFacts.tools` 走的是**注册表**，不是模型的叙述。

    主循环把 `registry.names()` 交给 L3 那一次审查（`_semantic_review`），
    「说某把工具不存在」那条判据才有记录可比——**不传就等于这条判据不存在**。
    """
    root = make_tree(tmp_path, {"a.py": "X = 1\n"})
    pending = PendingChanges(root)
    events: list = []
    model = _L3Model(
        turns=[
            _turn(
                "改一处",
                [
                    {
                        "name": "write_file",
                        "arguments": {"path": "a.py", "content": "X = 2\n"},
                        "why": "改",
                    }
                ],
            ),
            _turn("交卷", final="好了"),
        ],
        reviews=[
            payload(
                [
                    {
                        "file": "a.py",
                        "line": 1,
                        "problem": "这里用了不存在的工具 read_file，所以没改成",
                        "severity": "high",
                        "evidence": "工具不存在",
                    }
                ]
            )
        ],
    )
    loop = _loop(root, model, pending, events)

    loop.run("把 X 改成 2")

    # 审查者说「read_file 不存在」，而它在注册表里 → 那条被降级，不据此阻断。
    assert loop._semantic_report is not None, "L3 没跑：触发条件或接线有问题"
    assert loop._semantic_report.unverified == 1
    assert RS.CONFLICT_TOOL_MISSING in loop._semantic_report.conflicts
    assert loop._semantic_report.blocked is False


def test_说零改动被核对掉并改写成记录里的事实(tmp_path: Path) -> None:
    facts = RS.ReviewFacts(edits=2, changed=frozenset({"pipeline/fetch.py"}))
    finding = RS.SemanticFinding(
        file="pipeline/fetch.py",
        line=1,
        problem="这次运行 0 处改动，所以自动验证通过不能算数",
        severity=RS.SEVERITY_HIGH,
    )
    conflicts = RS.review_conflicts(finding, facts)

    assert [item.claim for item in conflicts] == ["声称这次运行 0 处改动"]
    assert "2 处改动落成" in conflicts[0].truth
    checked = RS.check_findings([finding], facts)
    assert checked.kept == ()
    assert len(checked.unverified) == 1
    downgraded = checked.unverified[0]
    assert downgraded.verified is False
    assert downgraded.blocking is False
    assert "与记录不符" in downgraded.render()


def test_说某处改动不在diff里被核对掉(tmp_path: Path) -> None:
    """L3 特有的第三种断言形状（在督导那两条之外）。"""
    facts = RS.ReviewFacts(edits=1, changed=frozenset({"a.py"}))
    finding = RS.SemanticFinding(
        file="a.py", line=3, problem="这处改动不在 diff 里", severity=RS.SEVERITY_HIGH
    )
    checked = RS.check_findings([finding], facts)

    assert checked.kept == ()
    assert checked.conflicts == {RS.CONFLICT_CHANGE_MISSING: 1}
    assert checked.unverified[0].blocking is False


def test_零改动时不核对这处改动找不到那条(tmp_path: Path) -> None:
    """反例：这一轮**真的**没有改动时，「diff 里没有」是对的，不该被改写。"""
    facts = RS.ReviewFacts(edits=0, changed=frozenset())
    finding = RS.SemanticFinding(
        file="a.py", line=1, problem="这处改动不在 diff 里", severity=RS.SEVERITY_LOW
    )
    assert RS.review_conflicts(finding, facts) == []


def test_位置查不到的发现降级而不删(tmp_path: Path) -> None:
    """凭空编一个工作区里没有的文件 → 降级，但**照样进报告**（§4）。"""
    make_tree(tmp_path, {"pipeline/fetch.py": "x = 1\n"})
    report = run_review(
        tmp_path,
        payload(
            [
                {
                    "file": "pipeline/根本没这个文件.py",
                    "line": 3,
                    "problem": "这里也漏了",
                    "severity": "high",
                    "evidence": "diff 里没有它",
                }
            ]
        ),
    )

    assert report.unverified == 1
    assert report.conflicts[RS.CONFLICT_UNKNOWN_FILE] == 1
    assert report.blocked is False
    # 同一条信息不删：它仍在发现清单里，只是被降级 + 标了「与记录不符」。
    assert len(report.findings) == 1
    assert report.findings[0].verified is False
    assert "与运行记录不符" in RS.render_unverified(report)


def test_工作区里真实存在的未改动文件不算查不到(tmp_path: Path) -> None:
    """**这是这一层最值钱的那条发现**：README 没在 diff 里，正因为它没改。"""
    make_tree(tmp_path, {"README.md": "# 改名说明：get_rows\n"})
    report = run_review(
        tmp_path,
        payload(
            [
                {
                    "file": "README.md",
                    "line": 1,
                    "problem": "README 里的 get_rows 没统一",
                    "severity": "high",
                    "evidence": "声称全仓统一，而 README 第 1 行仍是旧名",
                }
            ]
        ),
    )

    assert report.unverified == 0, [item.render() for item in report.findings]
    assert report.blocked is True
    assert report.findings[0].file == "README.md"


# ---- ③ 独立上下文（不喂自我陈述）---------------------------------------------


def test_送审提示里没有实现者的自我陈述() -> None:
    """结构化边界：材料里造不出「自我陈述」这个字段（§9.2）。"""
    fields = set(RS.ReviewMaterials.__dataclass_fields__)
    for banned in ("final", "summary", "self_report", "conclusion", "thought", "trace"):
        assert banned not in fields, f"送审材料里出现了 {banned}：那正是不能喂给审查者的东西"
    prompt = RS.build_prompt(
        RS.ReviewMaterials(
            goal="把 get_rows 改名成 load_rows",
            changes=(RS.ChangeFacts(path="a.py", edited=True, added=1, removed=1),),
            diff="--- a/a.py\n+++ b/a.py\n",
        )
    )
    # 提示里必须带用户请求、diff 与证据三类材料。
    assert "把 get_rows 改名成 load_rows" in prompt
    assert "--- a/a.py" in prompt
    assert "已经跑过的机械检查" in prompt
    assert "你看不到实现者的总结" in prompt


def test_循环送审材料里不含模型那一段收尾结论(tmp_path: Path) -> None:
    """真循环接线的那一半：`_semantic_materials` 不许把 `final` 拼进去。"""
    root = make_tree(
        tmp_path,
        {"src/main.py": "from util import get_rows\n\nprint(get_rows())\n"},
    )
    pending = PendingChanges(root)
    events: list = []
    model = _L3Model(
        turns=[
            _turn(
                "改名并更新调用点",
                [
                    {
                        "name": "write_file",
                        "arguments": {
                            "path": "src/main.py",
                            "content": "from util import load_rows\n\nprint(load_rows())\n",
                        },
                        "why": "统一改名",
                    }
                ],
            ),
            _turn("交卷", final="所有调用点都改成 load_rows 了，全仓没有残留"),
        ],
        reviews=[payload([], verdict="pass")],
    )
    loop = _loop(root, model, pending, events)

    loop.run("把 get_rows 改名成 load_rows，所有调用点都要改")

    material = loop._semantic_materials(["src/main.py"])
    prompt = RS.build_prompt(material)
    assert "所有调用点都改成 load_rows 了" not in prompt, "实现者的自我陈述被喂给审查者了"
    assert "全仓没有残留" not in prompt


# ---- ④ 触发条件（不该跑时不跑；不每步跑）-----------------------------------


def test_不该跑时一次模型调用都不发生(tmp_path: Path) -> None:
    calls: list[str] = []

    def chat(prompt: str) -> str:
        calls.append(prompt)
        return payload([{"file": "a.py", "line": 1, "problem": "x", "severity": "high"}])

    report = RS.review(materials(tmp_path), root=tmp_path, chat=chat)

    assert report.ran is False
    assert calls == [], "触发条件不满足时不许调用模型（§5：不是每步都跑）"
    assert "没有命中任何触发条件" in report.trigger
    assert "没有调用模型" in report.trigger
    assert report.findings == ()


@pytest.mark.parametrize(
    "kwargs, trigger",
    [
        ({"finding_count": 1}, "F1"),
        ({"regression": True}, "F2"),
        ({"files": 5}, "F3"),
        ({"lines": 400}, "F3"),
        ({"evidence_gaps": 1}, "F4"),
        ({"explicit": True}, "F5"),
    ],
)
def test_五条触发条件各自可证伪(kwargs: dict, trigger: str) -> None:
    decision = RS.decide_triggers(**kwargs)
    assert decision.run is True, kwargs
    assert any(item.startswith(trigger) for item in decision.reasons), decision.reasons


def test_触发线按旋钮走(tmp_path: Path) -> None:
    """规模线的旋钮真的管用：调低就触发、调高就不触发。"""
    assert RS.decide_triggers(files=4, max_files=5).run is False
    assert RS.decide_triggers(files=4, max_files=3).run is True
    assert RS.decide_triggers(lines=399, max_files=5).run is False
    assert RS.decide_triggers(lines=400, max_files=5).run is True


def test_同一个循环里同一份内容只审查一次(tmp_path: Path) -> None:
    """收尾的两条出口都会走到 L3：同一份内容必须复用同一份结论。"""
    root = make_tree(tmp_path, {"a.py": "X = 1\n"})
    pending = PendingChanges(root)
    events: list = []
    model = _L3Model(
        turns=[
            _turn(
                "改一处",
                [
                    {
                        "name": "write_file",
                        "arguments": {"path": "a.py", "content": "X = 2\n"},
                        "why": "改",
                    }
                ],
            ),
            _turn("交卷", final="改好了"),
        ],
        reviews=[payload([], verdict="pass")],
    )
    loop = _loop(root, model, pending, events)

    loop.run("改一处")
    first = loop._semantic_report
    loop._semantic_review(["a.py"])
    second = loop._semantic_report

    assert first is second, "同一份内容被审了两次（白花一次模型调用）"
    assert model.review_calls == 1


def test_发现条数超上限要说出来() -> None:
    many = [
        {"file": "a.py", "line": index + 1, "problem": f"问题{index}", "severity": "low"}
        for index in range(20)
    ]
    parsed = RS.parse_review(payload(many), max_findings=3)

    assert len(parsed.findings) == 3
    assert any("超过上限" in item for item in parsed.reasons), parsed.reasons


def test_diff截断要如实标明() -> None:
    body = "x" * 5000
    prompt = RS.build_prompt(
        RS.ReviewMaterials(
            goal="改一下",
            changes=(RS.ChangeFacts(path="a.py", edited=True),),
            diff=body,
            max_diff_chars=200,
        )
    )
    assert "已截断" in prompt
    assert len(prompt) < 4000


# ---- ⑤ 端到端：L2 抓不到而 L3 抓到 → 不许宣称完成，成果照落盘 ---------------


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


class _L3Model:
    """假 provider：把「回合」与「L3 审查」两条调用分开应答。

    判据是**提示词的开头那一句**，不是 `response_schema`——实测主循环的每一个
    回合请求**也带 schema**（`AgentLoop` 全程用 `chat_with_escalation`），
    而督导、L3 那两条都各自带自己的。按 schema 分流会把两者认成同一个。

    L3 的提示词由 `review_semantic.REVIEW_PROMPT` 产出，开头逐字是
    「你是一次独立代码审查」——按它分流最稳，而且它正是这一层的对外契约。
    """

    MARK = RS.REVIEW_PROMPT.split("\n", 1)[0][:12]

    def __init__(self, turns: list[str], reviews: list[str]) -> None:
        self._turns = list(turns)
        self._reviews = list(reviews)
        self._tokens = OfflineTokenCounter()
        self.turn_calls = 0
        self.review_calls = 0
        self.prompts: list[str] = []

    def context_window(self) -> int | None:
        return None

    def chat(self, request: ChatRequest) -> ChatResponse:
        prompt = request.messages[-1].content if request.messages else ""
        if prompt.startswith(self.MARK):
            self.review_calls += 1
            self.prompts.append(prompt)
            index = min(self.review_calls - 1, len(self._reviews) - 1)
            text = self._reviews[index]
        else:
            self.turn_calls += 1
            index = min(self.turn_calls - 1, len(self._turns) - 1)
            text = self._turns[index]
        return ChatResponse(
            text=text,
            prompt_tokens=sum(self._tokens.count(m.content) for m in request.messages),
            completion_tokens=self._tokens.count(text),
        )


def _loop(root: Path, model, pending, events: list | None = None, verify=None) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(list_dir_spec(root, pending))
    registry.register(read_file_spec(root, pending))
    register_edit_tools(registry, root, pending)
    return AgentLoop(
        gateway=model,
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(project_root=root, context_window=8192, max_steps=6),
        verify=verify,
        pending=pending,
        on_event=(
            (lambda kind, data: events.append((kind, data)))
            if events is not None
            else None
        ),
    )


def _always_green(changed) -> ToolResult:
    """一个「每次都验过」的假验证器（与 L2 用例里那个同一形状）。"""
    return ToolResult(ok=True, content="测试全过（假验证器）")


def test_L2抓不到而L3抓到并拦下完成声明(tmp_path: Path) -> None:
    """「声称所有调用点都改了」而 README 里旧名还在。

    L1/L2 都答不出这句话真假（L1 的残留检测当时跳过了本文件与已扫路径；
    L2 判的是「同一条命令、同一份输入，输出变没变」）。L3 拿 diff + 用户请求 +
    证据独立审一遍，就能指到 `README.md:1`。

    判据是**两侧都要成立**（§6 P2 验收第 7 条）：

      - `final_ok=False`：高严重度发现未处理 → 不许宣称完成；
      - 成果照落盘：`pending` 里那一处改动**仍在**（拦的是完成声明，不是成果）。
    """
    root = make_tree(
        tmp_path,
        {
            "README.md": "# 改名说明\n\n调用点用 `get_rows` 取行。\n",
            "src/main.py": "from util import get_rows\n\nprint(get_rows())\n",
        },
    )
    pending = PendingChanges(root)
    events: list = []
    model = _L3Model(
        turns=[
            _turn(
                "统一改名",
                [
                    {
                        "name": "write_file",
                        "arguments": {
                            "path": "src/main.py",
                            "content": "from util import load_rows\n\nprint(load_rows())\n",
                        },
                        "why": "把调用点统一成新名字",
                    }
                ],
            ),
            _turn("交卷", final="所有调用点都改成 load_rows 了，全仓已统一，没有残留"),
        ],
        reviews=[
            payload(
                [
                    {
                        "file": "README.md",
                        "line": 1,
                        "problem": "README 里的 get_rows 没统一（只改了 src/main.py）",
                        "severity": "high",
                        "suggestion": "README 里的旧名一起改掉",
                        "evidence": "diff 只有 src/main.py，而 README 第 3 行仍是 get_rows",
                    }
                ]
            )
        ],
    )
    loop = _loop(root, model, pending, events)

    result = loop.run("把 get_rows 改名成 load_rows，所有调用点都要改")

    # ① L3 真的跑了，而且只跑了**一次**（同一份内容不复审）。
    assert loop._semantic_report is not None
    assert loop._semantic_report.ran is True
    assert model.review_calls == 1
    # ② 高严重度发现 → 不许宣称完成。
    assert result.finished is False, result.final
    assert loop._review_blocked is True
    assert "被审查拦下" in result.final, result.final
    assert "README.md:1" in result.final, result.final
    # ③ 拦它的触发条件是 T9（L3 高严重度未处理），只降结论。
    triggered = {
        item.trigger for item in loop.escalation_gate.all_actions
    }
    assert "T9" in triggered, triggered
    # ④ 成果照落盘（§2.5 的硬不变量）：待确认清单里那一处改动仍在。
    assert [item.path for item in pending.items()] == ["src/main.py"]
    # ⑤ 事件流里的 `l3_blocked` 与结构化发现都在（探针按它们读）。
    kinds = [str(data.get("kind") or "") for kind, data in events if kind == "note"]
    assert "l3_blocked" in kinds, kinds
    assert "semantic_finding" in kinds, kinds
    detail = [
        data
        for kind, data in events
        if kind == "note" and data.get("kind") == "l3_detail"
    ]
    assert detail, kinds
    assert detail[0]["findings"][0]["file"] == "README.md"
    assert detail[0]["findings"][0]["severity"] == "high"
    assert detail[0]["findings"][0]["verified"] is True
    # ⑥ 反向条款在（模型可见文本），且不得读成「禁止删除」。
    assert "不是成果" in result.final
    assert "删死代码、合并重复实现、删已被替代的文件都是正常编辑" in result.final
    assert "禁止删除" not in result.final


def test_高严重度发现接在T9上且只降结论(tmp_path: Path) -> None:
    """L3 的发现接在**具名的触发条件 T9** 上，而且只降结论。

    为什么要单独钉 T9（不能只看 `finished=False`）：**T12 会一起命中**——
    L3 的发现也进同一个账本，所以「高严重度发现仍在」那条也会成立。于是
    「去掉高严重度即阻断」这个变异在 `finished` 上**看不出来**（第一次跑这条
    用例就抓到了这件事）。判据要看 T9 在不在：它才是「L3 高严重度未处理」
    这条语义的落点，也是探针 `escalation_by_trigger` 分档的依据。
    """
    root = make_tree(
        tmp_path,
        {
            "README.md": "# 说明\n\n调用点用 `get_rows`。\n",
            "src/main.py": "print(get_rows())\n",
        },
    )
    pending = PendingChanges(root)
    events: list = []
    model = _L3Model(
        turns=[
            _turn(
                "统一改名",
                [
                    {
                        "name": "write_file",
                        "arguments": {
                            "path": "src/main.py",
                            "content": "print(load_rows())\n",
                        },
                        "why": "改名",
                    }
                ],
            ),
            _turn("交卷", final="都改好了"),
        ],
        reviews=[
            payload(
                [
                    {
                        "file": "README.md",
                        "line": 3,
                        "problem": "README 里的旧名没统一",
                        "severity": "high",
                        "evidence": "diff 只有 src/main.py",
                    }
                ]
            )
        ],
    )
    loop = _loop(root, model, pending, events)

    loop.run("把 get_rows 改名成 load_rows")

    requests = loop.escalation_gate.all_actions
    triggers = {item.trigger for item in requests}
    assert "T9" in triggers, triggers
    t9 = next(item for item in requests if item.trigger == "T9")
    assert "README.md" in t9.paths
    # **纯结论级**：`action` 为空串 = 没有拦下任何动（成果照落盘，§3.2.2）。
    assert t9.action == ""
    # 探针与报告要的读数：L3 拦下的次数是运行的 0/1，且**成果仍在盘上**。
    kinds = [str(data.get("kind") or "") for kind, data in events if kind == "note"]
    assert kinds.count("l3_blocked") == 1
    assert [item.path for item in pending.items()] == ["src/main.py"]


def test_L3没触发时一次模型调用都不发生(tmp_path: Path) -> None:
    """反例（「不每步都跑」的可证伪读数）：没有触发条件时一次调用都不发生。

    这条直接钉**接线那一层**：主循环手上有发现条数、回归标志与账本缺口，
    三者都干净时必须判成「不跑」，并且读数如实写 0（不是静默——探针的
    `review_l3_ran` 就靠这条读数区分「没跑」与「跑了没报」）。

    为什么不在 `run()` 上钉：**第一次**宣告完成时上一个验证结论还是 None，
    账本如实报一条「没有证据的完成声明」→ 命中 F4（§5 第 4 条）。那一刻确实
    拿不到结论，触发是对的，不是缺陷；那条路走 `decide_triggers` 的
    `evidence_gaps` 那一条用例。这里把「上一版已验过」那个状态直接摆出来，
    量的是「干净的一轮会不会白花一次模型调用」。
    """
    root = make_tree(tmp_path, {"note.txt": "hello world\n"})
    pending = PendingChanges(root)
    events: list = []
    model = _L3Model(turns=[], reviews=[payload([], verdict="pass")])
    loop = _loop(root, model, pending, events, verify=_always_green)

    report = loop._semantic_review(["note.txt"], prior_verified=True)

    assert model.review_calls == 0, "不该跑时一次模型调用都不许发生"
    assert report is not None and report.ran is False
    assert loop._review_blocked is False
    # 「不该跑」这件事本身也要在事件流里看得见（读数如实写 0）。
    readings = [
        data
        for kind, data in events
        if kind == "note" and data.get("kind") == "l3_readings"
    ]
    assert readings and readings[0]["ran"] is False, readings
    assert "没有命中任何触发条件" in str(readings[0]["trigger"]), readings


def test_第一版还没有验证结论时按F4触发一次(tmp_path: Path) -> None:
    """上面那条的反面：**拿不到结论的那一轮确实要审一次**（F4）。

    这是「不许静默放行」的边界：没有任何可作证检查的一轮，正是语义审查该看一眼
    的那一轮。判据是它真的跑了一次（而不是被「省成本」省掉），且触发条件能回指
    §5 的第 4 条。
    """
    root = make_tree(tmp_path, {"note.txt": "hello\n"})
    pending = PendingChanges(root)
    events: list = []
    model = _L3Model(
        turns=[
            _turn(
                "写一份产出",
                [
                    {
                        "name": "write_file",
                        "arguments": {"path": "note.txt", "content": "hello world\n"},
                        "why": "改一下",
                    }
                ],
            ),
            _turn("交卷", final="改好了"),
        ],
        reviews=[payload([], verdict="pass")],
    )
    loop = _loop(root, model, pending, events, verify=_always_green)

    result = loop.run("把 note.txt 的内容改一下")

    assert model.review_calls == 1
    assert loop._semantic_report is not None
    assert loop._semantic_report.ran is True
    assert loop._semantic_report.trigger.startswith("F4")
    # 它没报发现 → 不拦完成声明（跑过 ≠ 一定要拦）。
    assert loop._review_blocked is False
    assert result.finished is True


def test_审查者说错话时那条不据以阻断(tmp_path: Path) -> None:
    """核对之后只剩降级项 → **完成声明不降级**（误报就是判据写错了，§4）。"""
    root = make_tree(
        tmp_path,
        {"src/main.py": "X = 1\n"},
    )
    pending = PendingChanges(root)
    events: list = []
    model = _L3Model(
        turns=[
            _turn(
                "改一处",
                [
                    {
                        "name": "write_file",
                        "arguments": {"path": "src/main.py", "content": "X = 2\n"},
                        "why": "改",
                    }
                ],
            ),
            _turn("交卷", final="改好了"),
        ],
        reviews=[
            payload(
                [
                    {
                        "file": "src/main.py",
                        "line": 9,
                        "problem": "这次运行 0 处改动，所以万事大吉",
                        "severity": "high",
                        "evidence": "diff 里没有",
                    },
                    {
                        "file": "src/查无此文件.py",
                        "line": 1,
                        "problem": "这里也漏了",
                        "severity": "high",
                        "evidence": "猜的",
                    },
                ]
            )
        ],
    )
    loop = _loop(root, model, pending, events)

    result = loop.run("把 X 改成 2")

    assert loop._semantic_report.unverified == 2
    assert loop._semantic_report.blocked is False
    assert loop._review_blocked is False
    assert result.finished is True
    assert "与运行记录不符" in result.final, result.final


# ---- ⑥ 文案纪律（§2.5）------------------------------------------------------


def test_L3的模型可见文本带反向条款() -> None:
    """§2.5 的硬规矩：模型可见字符串必须带反向条款，且不许读成「禁止删除」。"""
    source = Path(RS.__file__).read_text(encoding="utf-8")
    assert "不是成果" in source
    assert "删死代码、合并重复实现、删已被替代的文件都是正常编辑" in source
    assert "禁止删除" not in source
    # 提示词里也要有（`build_prompt` 真正发出去的那一段）。
    prompt = RS.build_prompt(
        RS.ReviewMaterials(
            goal="g",
            changes=(RS.ChangeFacts(path="a.py", edited=True),),
        )
    )
    assert "风格问题最多算" in prompt
