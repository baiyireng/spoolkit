"""互动面：把用户在网页壳里真实遇到的七个报告问题固化成回归断言。

## 这一组现在是绿的（六个问题都已修掉）

它钉的是"收尾报告该长什么样"。七个问题先在网页壳里被逐条指出，再在这一组里
**逐条落成断言**（断言本身一个字没动过：先前挂 `xfail(strict=True)` 是因为
问题还在，而 `strict=True` 会让"问题被修掉"变成 XPASS 失败——那正是
"该把 xfail 拆掉、让它转绿"的信号）。现在标记已拆，六条都是普通断言：

1. 撤销提示排在「等你确认/已应用」之前 → `test_撤销提示在文件层确认之后`（快照那条腿）
2. 同一条撤销信息出现两次 → `test_撤销提示只出现一次`
3. 无关样板：没拦动作却打 §2.5 反向条款 → `test_没拦动作时不出现反向条款`
4. 内部账漏进用户报告 → `test_不出现沿用那一次的结果`
5. 验证叙述自相矛盾 → `test_无验证器时不自相矛盾`
6. L2「查不了」铺一整段 → `test_查不了那一段不过长`

**判据一律不动**：每条钉的都是用户看得见的那份文本，判什么、阈值多少，
与修复前逐字一致（详见各条用例里保留的说明）。

## 两条腿

1. **假模型那条腿**（`_FakeRun`）：真循环 + `llm.fake.FakeModel` + 注入的验证器，
   **一次性进程内跑完**，拿 `LoopResult.final`（= 网页壳助手卡片里那一整段）。
   **单测绝不调模型**：假模型只按写死的脚本回放。
   实测复现：②③④⑤⑥（见每条用例里保留的 `reason` 文本）。
2. **快照那条腿**（`_load_snapshot`）：`scripts/interact_probe.py` 走 `spool mcp`
   真跑出来的快照。①（撤销提示排在「等你确认/已应用」之前）只在 CLI 结算那一段
   出现（`cli/commands/run.py` 先发 `revert_available` note、再由 `settle` 发
   `await`/`confirm`），进程内的假跑到不了那里；⑦（真正的答案被埋）要看真模型的
   结论怎么与结算叙述糊在一起。快照是**本地取证产物**（`.probe_runs/`，gitignored），
   没有它就 `fail` 说明怎么生成——用不存在的东西当判据比红更坏。

## 这个文件该放哪儿（重要）

按任务书它在 `tests/test_interact_probe.py`。**本次会话的沙箱把 `tests/` 设成
只读**（写入被拒，见交付说明），所以它先在 `.agent/interact_probe/` 里写成并跑通；
把它落到 `tests/test_interact_probe.py` 之后，`SNAPSHOT` 与断言一个字都不用改
（里面算的路径是"仓库根/.probe_runs/…"，与文件位置无关）。

## 生成快照（可选）

    python scripts/interact_probe.py --scenario create-hello \\
        --out .probe_runs/snapshot-create-hello.json
    python scripts/rejudge_snapshots.py      # 重新判一遍七条

再把快照裁剪成 `tests/fixtures/interact_probe/create-hello.json`
（`--fixture-out` 那一步就是它：只留 `goal` / `final_report` / `events` / `outcome`）：

    python scripts/interact_probe.py --scenario create-hello \\
        --out .probe_runs/snapshot-create-hello.json \\
        --fixture-out tests/fixtures/interact_probe/create-hello.json
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spoolkit.agent.loop import AgentLoop
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.edit import PendingChanges, write_file_spec
from spoolkit.tools.registry import ToolRegistry

#: 快照落点：`tests/fixtures/…`（推荐，进仓库的那一份）或探针的默认区。
#: 仓库根的算法对本文件放在哪儿都不敏感：
#:   `tests/test_interact_probe.py` → `parent.name == "tests"` → 上一层是仓库根；
#:   `.agent/interact_probe/test_interact_probe.py` → 上两层是仓库根。
_HERE = Path(__file__).resolve()
_REPO = (
    _HERE.parents[1] if _HERE.parent.name == "tests" else _HERE.parents[2]
)
SNAPSHOT_CANDIDATES = (
    _REPO / "tests" / "fixtures" / "interact_probe" / "create-hello.json",
    _REPO / ".probe_runs" / "fixtures" / "interact_probe" / "create-hello.json",
    _REPO / ".probe_runs" / "snapshot-create-hello.json",
)

#: 与真实运行逐字一致的验证器回话：只写了一个 `.txt` 时
#: `tools/verify.py::lazy_file_verify` 走的就是这一句（ok=True）。
NO_CHECKER_LINE = "这次改的是 .txt，没有对应的自动验证器。"

#: 「撤销提示只出现一次」里认的那条命令。
REVERT_MARK = "spool revert --run"


# ---- 假模型那条腿 -----------------------------------------------------------

def _turn(calls, final=None) -> str:
    return json.dumps(
        {
            "thought": "think",
            "tool_calls": calls,
            "state": None,
            "done": final is not None,
            "final": final,
        },
        ensure_ascii=False,
    )


class _FakeRun:
    """一次进程内假跑的读数：报告正文 + 事件流。"""

    def __init__(self, final: str, events: list[tuple[str, dict]]) -> None:
        self.final = final
        self.events = events

    def notes(self) -> list[str]:
        return [str(data.get("text") or "")
                for kind, data in self.events if kind == "note"]

    def tools(self) -> list[tuple[str, bool, str]]:
        return [
            (str(data.get("name") or ""), bool(data.get("ok")),
             str(data.get("detail") or ""))
            for kind, data in self.events
            if kind == "tool"
        ]

    def undo_records(self) -> list[str]:
        """用户可见的撤销提示：只有「回退这一轮」与「撤销这一轮」那两句。

        请示五件摘要里的 `⑤ 回退：spool revert --run …` **不算**——那是被拦下
        那个动作的退路，跟"这一轮成果的撤销提示"不是一件事（判据要跟
        `scripts/interact_probe.py::judge` 一致，否则同一条问题两个说法）。
        """
        return [
            note for note in self.notes()
            if REVERT_MARK in note and ("回退这一轮" in note or "撤销这一轮" in note)
        ]


@pytest.fixture
def fake_run(tmp_path: Path) -> _FakeRun:
    """真循环 + 假模型：写一个 hello.txt，然后宣告完成。

    验证器是**注入的**（不是 `make_verifier`）：`make_verifier` 只在 node 或
    pytest 在的时候才装配得出来，而这条用例钉的是"报告怎么说话"，不该依赖
    机器上有什么。注入的回话与真实那一句逐字一致。
    """
    from spoolkit.tools.types import ToolResult

    (tmp_path / "README.md").write_text(
        "# 小项目\n\n还没有用法示例。\n", encoding="utf-8"
    )
    pending = PendingChanges(tmp_path)
    registry = ToolRegistry()
    registry.register(write_file_spec(tmp_path, pending))
    tokenizer = OfflineTokenCounter()
    seen: list[tuple[str, dict]] = []
    calls = {"verify": 0}

    def verifier(_changed=()):
        calls["verify"] += 1
        return ToolResult(ok=True, content=NO_CHECKER_LINE)

    loop = AgentLoop(
        gateway=FakeModel(
            script=[
                _turn([{
                    "name": "write_file",
                    "arguments": {"path": "hello.txt", "content": "Hello Word"},
                }]),
                _turn([], final="已在当前目录创建 hello.txt，内容为 Hello Word。"),
            ],
            tokenizer=tokenizer,
        ),
        tokenizer=tokenizer,
        registry=registry,
        config=Config(
            project_root=tmp_path,
            context_window=4096,
            max_steps=6,
            supervise=False,
        ),
        on_event=lambda kind, data: seen.append((kind, data)),
        verify=verifier,
        pending=pending,
    )
    result = loop.run("帮我在当前目录创建一个文本文件，上面写上 Hello Word")
    assert calls["verify"] >= 1, "假跑必须真的走过一次自动验证，否则判据没有材料"
    return _FakeRun(result.final or "", seen)


# ---- 快照那条腿 -------------------------------------------------------------

def load_snapshot() -> dict:
    for path in SNAPSHOT_CANDIDATES:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    pytest.fail(
        "没有互动面快照（先跑 `python scripts/interact_probe.py --scenario "
        "create-hello --fixture-out tests/fixtures/interact_probe/create-hello.json`）："
        "这一条判据的材料在快照里，没有材料就如实红，不假装通过。"
    )
    raise AssertionError("unreachable")


def snapshot_lines(snapshot: dict) -> list[str]:
    """快照 → 用户看到的那一串行（与 `interact_probe.render_user_text` 同口径）。

    `await` / `confirm` 也算：壳把它们渲染成「等你确认：N 处改动」与
    「已应用 N 处改动」（`web/runner.py::trace_line`）。命令授权那两条
    （带 `command`）排除——它们在第 0 步就发了，与撤销提示的先后不是用户
    抱怨的那件事。
    """
    lines: list[str] = []
    for event in snapshot.get("events") or []:
        kind = event.get("type")
        if kind == "note":
            lines.append(str(event.get("text") or ""))
        elif kind == "tool":
            detail = str(event.get("detail") or "")
            lines.append(
                f"{event.get('name', '?')} → {'成功' if event.get('ok') else '失败'}"
                + (f"：{detail}" if detail else "")
            )
        elif kind == "await":
            command = str(event.get("command") or "").strip()
            lines.append(
                f"等你确认：要执行 {command}" if command
                else f"等你确认：{event.get('count', 1)} 处改动"
            )
        elif kind == "confirm":
            body = (f"{'已应用' if event.get('applied') else '已拒绝'} "
                    f"{event.get('count', 0)} 处改动")
            if str(event.get("command") or "").strip():
                body += f"（命令授权：{event['command']}）"
            lines.append(body)
        elif kind == "final":
            lines.append(str(event.get("text") or ""))
    flat: list[str] = []
    for item in lines:
        flat.extend(item.splitlines() or [item])
    return flat


def _is_file_level_wait(line: str) -> bool:
    return (
        ("等你确认" in line or line.startswith("已应用")
         or line.startswith("已拒绝"))
        and "处改动" in line
        and "要执行" not in line
        and "命令授权" not in line
    )


# ---- 七个问题 ---------------------------------------------------------------

def test_撤销提示在文件层确认之后() -> None:
    """问题①：撤销提示必须排在「等你确认/已应用」之后。

    修复前的实测：撤销在第 8 行、文件层确认在第 9 行——用户先看到退路、
    后看到结果。现在撤销那一行落在收尾报告（`final` 事件）里，而 `final`
    是这条通道最后发的一条，所以它天然排在 `await` / `confirm` 之后。
    """
    snapshot = load_snapshot()
    lines = snapshot_lines(snapshot)
    undo_pos = next((i for i, line in enumerate(lines) if REVERT_MARK in line), -1)
    wait_pos = next((i for i, line in enumerate(lines)
                     if _is_file_level_wait(line)), -1)
    assert undo_pos >= 0, "快照里没有撤销提示：判据本身没材料了"
    assert wait_pos >= 0, "快照里没有文件层的「等你确认/已应用 N 处改动」"
    assert undo_pos > wait_pos, (
        f"撤销提示排在第 {undo_pos + 1} 行，而文件层确认在第 {wait_pos + 1} 行"
        "——撤销提示跑到\"等你确认/已应用\"前面去了，用户先看到退路、后看到结果。"
    )


def test_撤销提示只出现一次(fake_run: _FakeRun) -> None:
    """问题②：同一条撤销信息只许出现一次。

    修复前：事件流一条 `revert_available` note + 报告尾部一条，同一件事说两遍。
    现在撤销提示**只有报告里那一行**（⑤ 可做的动作），事件流不再另发一条。
    """
    body = fake_run.final
    note_hits = fake_run.undo_records()
    report_hits = [
        line for line in body.splitlines()
        if REVERT_MARK in line and ("回退这一轮" in line or "撤销这一轮" in line)
    ]
    total = len(note_hits) + len(report_hits)
    assert total == 1, (
        f"撤销提示出现了 {total} 次：事件流 {len(note_hits)} 次、报告 "
        f"{len(report_hits)} 次\n事件流：{note_hits}\n报告：{report_hits}"
    )


def test_没拦动作时不出现反向条款(fake_run: _FakeRun) -> None:
    """问题③：什么都没拦时，§2.5 的反向条款一个字都不该出现。

    修复前：`L2 行为回归读数` 那一段固定带「…**不是成果**：改动照落盘…
    删死代码…」，而这一轮什么都没拦——那是无关样板。现在那一段**按需出现**：
    既没有 L2 实测读数、改的又是 `.txt` 这类没有行为面的产出时，整段不出。
    """
    body = fake_run.final
    blocked = "被审查拦下" in body
    escalated = "【待批复】" in body or "触发条件" in body
    has_clause = "不是成果" in body and (
        "删死代码" in body or "改动照落盘" in body
    )
    assert not has_clause or blocked or escalated, (
        "这一轮没有任何动作被拦下，报告里却出现了 §2.5 的反向条款"
        "（「拦的是完成声明与危险动作，不是成果…删死代码…」）：它是无关样板。\n"
        f"报告：{body}"
    )


def test_不出现沿用那一次的结果(fake_run: _FakeRun) -> None:
    """问题④：内部账只进 trace/事件，不进用户读的那一份报告。

    修复前那一句是「（上一步验过之后这些文件的内容一字未变，所以沿用那一次
    的结果）」——它是"为什么这次没重跑"的实现细节，报告只说结论。
    """
    assert "沿用那一次的结果" not in fake_run.final, (
        "报告里出现了内部账「（上一步验过之后这些文件的内容一字未变，所以沿用"
        "那一次的结果）」——那是实现细节，不该出现在用户读的那一份里。\n"
        f"报告：{fake_run.final}"
    )
    assert "沿用最近一次" not in fake_run.final


def test_无验证器时不自相矛盾(fake_run: _FakeRun) -> None:
    """问题⑤：先「自动验证 → 成功」、后又说"没有对应的自动验证器"。

    同一个 `.txt` 上跑不出机械判据，而验证器如实说了这一点——把它报成
    「成功」等于告诉用户"验过了"。现在这一档说「没有校验器（不是失败）」，
    事件流里也不再发一条 `自动验证 → 成功`。
    """
    auto = [item for item in fake_run.tools() if item[0] == "自动验证"]
    says_none = "没有对应的自动验证器" in fake_run.final
    assert not (auto and says_none), (
        "同一次运行里既发了「自动验证 → 成功」"
        f"（{auto[0][2] if auto else ''}），"
        "报告里又说「没有对应的自动验证器」——两句话自相矛盾。\n"
        f"报告：{fake_run.final}"
    )


def test_查不了那一段不过长(fake_run: _FakeRun) -> None:
    """问题⑥：L2「查不了」最多一行、且不超过 100 字。

    修复前对一次只写了一个 `.txt` 的运行铺了 114 字一整段。现在它按需出现
    （没有行为面就不出现），真的要说时也压成一行、且保留「找不到可跑的对照」
    这个理由。
    """
    lines = [line for line in fake_run.final.splitlines() if "【查不了】" in line]
    assert not lines or (len(lines) == 1 and len(lines[0]) <= 100), (
        "L2 的「查不了」占了 "
        f"{len(lines)} 行、最长 {max((len(line) for line in lines), default=0)} 字；"
        "一次只写了一个 .txt 的运行不该铺这么长。\n" + "\n".join(lines)
    )


def test_结论行含_hello_txt_且独立成行() -> None:
    """问题⑦：真正的答案被埋。

    **这一条现在就是绿的，不是 xfail**：`create-hello` 的快照里，答案那一句
    （`已在当前目录创建 hello.txt，内容为 Hello Word。`）确实是报告的第一行、
    也没跟结算叙述糊在同一行——这一路**没复现**问题⑦（详见交付报告里那一条
    判定的"这次没看到"）。所以按"不许为了让测试绿而放宽断言"的同一条纪律，
    也不该为了凑数给它挂 xfail（那会变成 XPASS(strict) 失败）。

    留下它是因为它是**正向要求**：答案必须在、而且是独立的一句。一旦报告改成
    把答案埋进结算叙述里（或整份报告只剩"本次运行共落成 N 处改动"），这条会红。
    """
    snapshot = load_snapshot()
    report = str(snapshot.get("final_report") or "")
    settlement_marks = ("本次运行共落成", "最终文件状态", "最终验证：")
    answer_lines = [
        line for line in report.splitlines()
        if ("创建" in line or "已创建" in line) and ".txt" in line
    ]
    assert answer_lines, f"报告里没有一句说清 .txt 建出来了：\n{report}"
    standalone = [
        line for line in answer_lines
        if not any(mark in line for mark in settlement_marks)
    ]
    assert standalone, (
        "用户那一句结论跟结算叙述糊在同一行里（"
        f"{answer_lines[0][:200]}）——「文件建好没有」这个答案得在一句独立的话里。"
    )


def test_快照本身有材料() -> None:
    """快照是取证材料，它得真的有现场（否则上面两条快照判据是空断言）。"""
    snapshot = load_snapshot()
    report = str(snapshot.get("final_report") or "")
    assert "hello.txt" in report, "快照的最终报告里没有 hello.txt：快照生成错了"
    assert snapshot.get("events"), "快照里没有事件流"
    assert snapshot.get("outcome"), "快照里没有结局读数"
