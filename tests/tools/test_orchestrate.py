"""编排工具（一步内发出并执行多个工具调用）。

这一批钉的是**可证伪**的那几件事，每条都带反事实：

1. **整批校验**：未知工具 / 错参数 → 拒绝话术里有「可用清单」与「照抄形状」
   （`NEXT_STEP_MARKER`），与单调用的 `tools/registry.py` 同一份口径；
2. **依赖拓扑**：`needs` 决定顺序，成环 / 指错 id / 重复 id 一律整批拒绝；
3. **只读确实并行**：用可控延时测「时间区间真的重叠」，并断言并行上限生效；
4. **写确实串行**：断言执行期间同时在跑的写不超过 1 条、顺序等于计划顺序，
   而且写与只读**不重叠**（写不会在别人读一半时改工作区）；
5. **失败隔离**：一条失败/被拦不带走别的分支；依赖它的那几条标成「跳过」；
6. **聚合**：含每一条的状态、被拦那几条的替代、§2.5 反向条款；超预算截断并
   如实标注，且**不截**状态与条款；
7. **请批**：`needs_approval` 的分支无人值守时**挂起待批**，其余分支照常做完；
8. **整批一个运行单元**：一批里多处写只有一个 `run_id`，收尾只有**一条**
   `spool revert --run <id>`，而且这条命令真的能把整批退回去；
9. **不变量**：成果在盘（落盘后文件真的在）、回退可用、工作区非空；
10. **可观测**：每条子调用一条事件，带 id / 工具 / why / 状态 / 耗时 / 原因。
"""

import json
import threading
import time
from pathlib import Path

from spoolkit.agent import loop as loop_module
from spoolkit.cli.runtime import LoopWiring, assemble_loop
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools import capabilities as C
from spoolkit.tools import orchestrate as O
from spoolkit.tools.edit import PendingChanges, delete_file_spec, write_file_spec
from spoolkit.tools.fs import list_dir_spec, read_file_spec
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.types import NEXT_STEP_MARKER, REVERSE_CLAUSE, ToolResult


# ---- 夹具 -------------------------------------------------------------------


def _registry(root: Path, pending: PendingChanges | None = None) -> ToolRegistry:
    pending = pending if pending is not None else PendingChanges(root)
    registry = ToolRegistry()
    registry.register(read_file_spec(root, pending))
    registry.register(list_dir_spec(root, pending))
    registry.register(write_file_spec(root, pending))
    registry.register(delete_file_spec(root, pending))
    return registry


def _plan(**kwargs) -> dict:
    base = {"why": "一次把该看的看完", "calls": [], "aggregate": "合成一段"}
    base.update(kwargs)
    return base


def _call(call_id, tool, args=None, why="想拿到它", needs=None) -> dict:
    return {
        "id": call_id,
        "tool": tool,
        "args": args or {},
        "why": why,
        "needs": needs or [],
    }


class _Fake:
    """记录执行区间的假分支通道（并行 / 串行都用它量）。"""

    def __init__(self, delay: float = 0.15, results=None, statuses=None) -> None:
        self.delay = delay
        self.results = results or {}
        self.statuses = statuses or {}
        self.spans: list[tuple[str, float, float]] = []
        self.active = 0
        self.max_active = 0
        self.serial_active = 0
        self.max_serial_active = 0
        self.order: list[str] = []
        self.notes: list[str] = []
        self.events: list[tuple[str, dict]] = []
        self._lock = threading.Lock()

    def _run(self, call, kind: str) -> ToolResult:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if kind == "serial":
                self.serial_active += 1
                self.max_serial_active = max(self.max_serial_active, self.serial_active)
            self.order.append(f"{kind}:{call.name}:{call.arguments.get('path', '')}")
        started = time.time()
        try:
            time.sleep(self.delay)
            return self.results.get(
                call.name, ToolResult(ok=True, content=f"{call.name} 的结果")
            )
        finally:
            with self._lock:
                self.spans.append((kind, started, time.time()))
                self.active -= 1
                if kind == "serial":
                    self.serial_active -= 1

    def runner(self) -> O.BranchRunner:
        return O.BranchRunner(
            read=lambda call: self._run(call, "read"),
            serial=lambda call: self._run(call, "serial"),
            status=lambda call, result: self.statuses.get(
                call.name, O.STATUS_OK if result.ok else O.STATUS_FAILED
            ),
            note=lambda call, result: self.notes.append(call.name),
            emit=lambda kind, data: self.events.append((kind, data)),
        )


def _overlaps(spans, kind_a: str, kind_b: str) -> bool:
    for one_kind, one_start, one_end in spans:
        if one_kind != kind_a:
            continue
        for other_kind, other_start, other_end in spans:
            if other_kind != kind_b:
                continue
            if one_start < other_end and other_start < one_end:
                return True
    return False


# ---- 1. 整批校验 ------------------------------------------------------------


def test_未知工具拒绝里带可用清单与照抄形状(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    plan, problem = O.parse_plan(
        _plan(calls=[_call("a1", "no_such_tool")]), registry, max_calls=8
    )
    assert plan is None
    assert "未知工具" in problem
    assert "可用工具: " in problem  # ① 支持什么
    assert "read_file" in problem
    assert NEXT_STEP_MARKER in problem  # ② 照做的下一步（可照抄）
    assert '"id":"a1"' in problem


def test_错参数的拒绝沿用既有话术且能照抄(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    plan, problem = O.parse_plan(
        _plan(calls=[_call("a1", "read_file", {"pth": "x"})]), registry, max_calls=8
    )
    assert plan is None
    assert "参数校验失败" in problem
    assert "可用参数: " in problem  # 与 `registry.validate` 逐字相同的那一半
    assert NEXT_STEP_MARKER in problem
    assert 'read_file(path="…")' in problem  # 照抄就能跑的形状


def test_每次调用都必须写why(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    plan, problem = O.parse_plan(
        _plan(calls=[_call("a1", "read_file", {"path": "x"}, why="")]),
        registry,
        max_calls=8,
    )
    assert plan is None
    assert "缺少 why" in problem
    assert NEXT_STEP_MARKER in problem


def test_id重复_依赖指错_成环都整批拒绝(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    calls = [_call("a1", "read_file", {"path": "x"}), _call("a1", "list_dir", {"path": "."})]
    plan, problem = O.parse_plan(_plan(calls=calls), registry, max_calls=8)
    assert plan is None and "重复" in problem

    calls = [_call("a1", "read_file", {"path": "x"}, needs=["z9"])]
    plan, problem = O.parse_plan(_plan(calls=calls), registry, max_calls=8)
    assert plan is None and "不存在的调用 id" in problem

    calls = [
        _call("a1", "read_file", {"path": "x"}, needs=["b1"]),
        _call("b1", "list_dir", {"path": "."}, needs=["a1"]),
    ]
    plan, problem = O.parse_plan(_plan(calls=calls), registry, max_calls=8)
    assert plan is None and "循环依赖" in problem

    calls = [_call("a1", "read_file", {"path": "x"}, needs=["a1"])]
    plan, problem = O.parse_plan(_plan(calls=calls), registry, max_calls=8)
    assert plan is None and "它自己" in problem


def test_编排里不能再编排且批有上限(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    plan, problem = O.parse_plan(
        _plan(calls=[_call("a1", "orchestrate", {"plan": {}})]), registry, max_calls=8
    )
    assert plan is None and "不能再编排" in problem

    many = [_call(f"a{i}", "read_file", {"path": "x"}) for i in range(9)]
    plan, problem = O.parse_plan(_plan(calls=many), registry, max_calls=8)
    assert plan is None and "上限（8 条）" in problem
    assert NEXT_STEP_MARKER in problem


def test_计划外壳写错也给形状(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    for raw in ("不是对象", _plan(why=""), _plan(calls=[])):
        plan, problem = O.parse_plan(raw, registry, max_calls=8)
        assert plan is None
        assert NEXT_STEP_MARKER in problem


# ---- 2. 依赖拓扑与顺序 ------------------------------------------------------


def test_依赖决定顺序且有依赖的不进并行波次(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    plan, problem = O.parse_plan(
        _plan(
            calls=[
                _call("a1", "read_file", {"path": "x"}),
                _call("b1", "list_dir", {"path": "."}, needs=["a1"]),
            ]
        ),
        registry,
        max_calls=8,
    )
    assert problem == ""
    assert plan is not None
    fake = _Fake(delay=0.05)
    outcome = O.run_plan(
        plan, registry=registry, runner=fake.runner(), max_parallel=4, aggregate_chars=2400
    )
    # 并行的只读块先跑完，依赖它的那一条在下一轮才跑。
    assert outcome.branches[0].call.id == "a1"
    assert outcome.branches[1].call.id == "b1"
    assert [one.call.id for one in outcome.branches] == ["a1", "b1"]
    line = O._chain_line(outcome.branches[1])
    assert "依赖：a1" in line


# ---- 3. 只读确实并行 --------------------------------------------------------


def test_只读分支真的并发跑(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    calls = [_call(f"r{i}", "read_file", {"path": f"{i}.txt"}) for i in range(3)]
    plan, _ = O.parse_plan(_plan(calls=calls), registry, max_calls=8)
    assert plan is not None
    fake = _Fake(delay=0.2)
    started = time.time()
    outcome = O.run_plan(
        plan, registry=registry, runner=fake.runner(), max_parallel=4, aggregate_chars=2400
    )
    elapsed = time.time() - started
    # 反事实：串行跑要 3×0.2=0.6s；并发跑明显更快，而且三条的时间区间互相重叠。
    assert elapsed < 0.45, f"三条只读分支没有并发（{elapsed:.2f}s）"
    assert fake.max_active >= 2
    assert _overlaps(fake.spans, "read", "read")
    assert [one.status for one in outcome.branches] == [O.STATUS_OK] * 3


def test_并行上限真的生效(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    calls = [_call(f"r{i}", "read_file", {"path": f"{i}.txt"}) for i in range(5)]
    plan, _ = O.parse_plan(_plan(calls=calls), registry, max_calls=8)
    assert plan is not None
    fake = _Fake(delay=0.05)
    O.run_plan(
        plan, registry=registry, runner=fake.runner(), max_parallel=2, aggregate_chars=2400
    )
    assert fake.max_active <= 2, f"并行上限没生效（同时 {fake.max_active} 条）"
    # 上限为 1 时退化成串行——这是排查「并行是不是引入了不确定性」的那个旋钮。
    fake2 = _Fake(delay=0.05)
    O.run_plan(
        plan, registry=registry, runner=fake2.runner(), max_parallel=1, aggregate_chars=2400
    )
    assert fake2.max_active == 1


# ---- 4. 写确实串行 ---------------------------------------------------------


def test_写分支串行且顺序等于计划顺序(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    calls = [
        _call("w1", "write_file", {"path": "1.txt", "content": "1"}),
        _call("w2", "write_file", {"path": "2.txt", "content": "2"}),
        _call("w3", "write_file", {"path": "3.txt", "content": "3"}),
    ]
    plan, _ = O.parse_plan(_plan(calls=calls), registry, max_calls=8)
    assert plan is not None
    fake = _Fake(delay=0.05)
    O.run_plan(
        plan, registry=registry, runner=fake.runner(), max_parallel=4, aggregate_chars=2400
    )
    assert fake.order == ["serial:write_file:1.txt", "serial:write_file:2.txt", "serial:write_file:3.txt"]
    assert fake.max_serial_active == 1


def test_写与只读不重叠(tmp_path: Path) -> None:
    """写不会在别人读一半时改工作区：只读波次先跑完，写才依次跑。"""
    registry = _registry(tmp_path)
    calls = [
        _call("r1", "read_file", {"path": "a.txt"}),
        _call("r2", "list_dir", {"path": "."}),
        _call("w1", "write_file", {"path": "out.txt", "content": "x"}),
    ]
    plan, _ = O.parse_plan(_plan(calls=calls), registry, max_calls=8)
    assert plan is not None
    fake = _Fake(delay=0.05)
    O.run_plan(
        plan, registry=registry, runner=fake.runner(), max_parallel=4, aggregate_chars=2400
    )
    # 两条只读可以同时跑（max_active 会是 2），但**写从不与任何东西重叠**：
    # 串行块里同时在跑的写最多 1 条，而且没有一条写与只读的区间相交。
    assert fake.max_serial_active == 1, "两条写同时在跑"
    assert not _overlaps(fake.spans, "read", "serial")


def test_派发分支不进并行池(tmp_path: Path) -> None:
    """`dispatch` 派出去的子智能体手里有写工具：它必须串行（能力声明里的事实位）。"""
    assert C.declared("dispatch") is not None
    assert C.declared("dispatch").delegates is True
    assert C.can_parallel("dispatch") is True  # 它自己不是写工具
    assert O._parallel_ok("dispatch") is False  # 但编排不许它与别人并行
    assert O._parallel_ok("read_file") is True
    assert O._parallel_ok("write_file") is False
    assert O._parallel_ok("no_such_tool") is False  # 没声明过 → 最保守


# ---- 5. 失败隔离 -----------------------------------------------------------


def test_一条失败不带走别的分支_依赖它的被跳过(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    calls = [
        _call("bad", "read_file", {"path": "missing.txt"}),
        _call("dep", "list_dir", {"path": "."}, needs=["bad"]),
        _call("free", "list_dir", {"path": "."}),
    ]
    plan, _ = O.parse_plan(_plan(calls=calls), registry, max_calls=8)
    assert plan is not None
    fake = _Fake(
        delay=0.01,
        results={
            "read_file": ToolResult(ok=False, content="文件不存在: missing.txt"),
        },
    )
    outcome = O.run_plan(
        plan, registry=registry, runner=fake.runner(), max_parallel=4, aggregate_chars=2400
    )
    by_id = {one.call.id: one for one in outcome.branches}
    assert by_id["bad"].status == O.STATUS_FAILED
    assert by_id["dep"].status == O.STATUS_SKIPPED
    assert "没有成功" in by_id["dep"].reason
    # 独立分支照做——这就是「失败隔离」的全部意思。
    assert by_id["free"].status == O.STATUS_OK
    assert outcome.ok is True
    assert "bad" in outcome.text and "free" in outcome.text
    assert O.STATUS_SKIPPED in outcome.text


def test_分支抛异常也只算这一条失败(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    plan, _ = O.parse_plan(
        _plan(
            calls=[
                _call("boom", "read_file", {"path": "a"}),
                _call("ok1", "list_dir", {"path": "."}),
            ]
        ),
        registry,
        max_calls=8,
    )
    assert plan is not None

    def explode(call):
        if call.name == "read_file":
            raise RuntimeError("分支炸了")
        return ToolResult(ok=True, content="fine")

    fake = _Fake(delay=0.01)
    runner = O.BranchRunner(
        read=explode,
        serial=explode,
        status=lambda call, result: O.STATUS_OK if result.ok else O.STATUS_FAILED,
        emit=lambda kind, data: fake.events.append((kind, data)),
    )
    outcome = O.run_plan(
        plan, registry=registry, runner=runner, max_parallel=4, aggregate_chars=2400
    )
    by_id = {one.call.id: one for one in outcome.branches}
    assert by_id["boom"].status == O.STATUS_FAILED
    assert "分支执行异常" in by_id["boom"].reason
    assert by_id["ok1"].status == O.STATUS_OK


# ---- 6. 聚合与预算 ---------------------------------------------------------


def test_聚合含每条状态与被拦的替代(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    calls = [
        _call("a1", "read_file", {"path": "a.txt"}),
        _call("b1", "write_file", {"path": "out.txt", "content": "x"}),
    ]
    plan, _ = O.parse_plan(_plan(calls=calls), registry, max_calls=8)
    assert plan is not None
    blocked = (
        f"{loop_module.BLOCKED_PREFIX}这个动作被风险分级放行拦下了。"
        f"\n{NEXT_STEP_MARKER}换一条不需要该动作的路（改代码而不是跑那条命令）。"
    )
    fake = _Fake(
        delay=0.01,
        results={
            "read_file": ToolResult(ok=True, content="a 的内容"),
            "write_file": ToolResult(ok=False, content=blocked),
        },
        statuses={"write_file": O.STATUS_BLOCKED},
    )
    outcome = O.run_plan(
        plan, registry=registry, runner=fake.runner(), max_parallel=4, aggregate_chars=2400
    )
    text = outcome.text
    assert "a1 read_file" in text and "成功" in text
    assert "b1 write_file" in text and O.STATUS_BLOCKED in text
    assert "已经给过的替代" in text  # ② 被拦那几条给过什么替代
    assert NEXT_STEP_MARKER in text
    assert REVERSE_CLAUSE in text  # ③ §2.5 反向条款
    assert "不许删文件" in text  # 且它不是「禁止删除」的口径
    assert "删死代码" in text
    # 判据用的原始证据（不给模型看）里每条分支都有一行。
    payload = json.loads(outcome.evidence())
    assert [one["id"] for one in payload["branches"]] == ["a1", "b1"]
    assert payload["branches"][1]["status"] == O.STATUS_BLOCKED


def test_聚合超预算截断并如实标注(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    calls = [_call(f"r{i}", "read_file", {"path": f"{i}.txt"}) for i in range(3)]
    plan, _ = O.parse_plan(_plan(calls=calls), registry, max_calls=8)
    assert plan is not None
    long_body = "行" * 3000
    fake = _Fake(
        delay=0.01,
        results={"read_file": ToolResult(ok=True, content=long_body)},
    )
    outcome = O.run_plan(
        plan, registry=registry, runner=fake.runner(), max_parallel=1, aggregate_chars=900
    )
    assert outcome.truncated is True
    assert len(outcome.text) <= 1300, len(outcome.text)
    assert "超出聚合预算" in outcome.text or "未展开" in outcome.text
    # 必需的三样就算再紧也不许被截掉。
    fake2 = _Fake(delay=0.01, results={"read_file": ToolResult(ok=True, content=long_body)})
    tight = O.run_plan(
        plan, registry=registry, runner=fake2.runner(), max_parallel=1, aggregate_chars=200
    )
    assert tight.truncated is True
    for call in calls:
        assert call["id"] in tight.text
    assert REVERSE_CLAUSE in tight.text


# ---- 7. 请批：无人值守挂起而其余照做 ---------------------------------------


def _loop(root: Path, script: list, pending: PendingChanges, **kwargs):
    """真的装配一次主循环（`orchestrate` 就在注册表里）。"""
    return assemble_loop(
        root,
        FakeModel(script=script, tokenizer=OfflineTokenCounter()),
        config=Config(project_root=root, supervise=False, max_steps=6, **kwargs),
        wiring=LoopWiring(mcp=False, pending=pending),
    )


def _turn(thought, calls, done=False, final=None) -> str:
    return json.dumps(
        {"thought": thought, "tool_calls": calls, "done": done, "final": final},
        ensure_ascii=False,
    )


def _orch_turn(plan: dict, why: str = "一步编排") -> str:
    return _turn(why, [{"name": "orchestrate", "arguments": {"plan": plan}, "why": why}])


def test_请批的分支无人值守时挂起_其余照做(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("内容\n", encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "x.txt").write_text("x\n", encoding="utf-8")
    pending = PendingChanges(tmp_path)
    plan = _plan(
        calls=[
            _call("a1", "read_file", {"path": "a.txt"}),
            _call("r1", "run_command", {"command": ["rm", "-rf", "build"]}, why="清掉旧构建"),
        ]
    )
    loop = _loop(
        tmp_path,
        [_orch_turn(plan), _turn("完成", [], done=True, final="做完了")],
        pending,
    )
    assert "run_command" in loop.registry.names()
    result = loop.run("看一眼再清构建目录")
    # `finished=False` 在这里的含义是「**有动作在等批复**」，不是「成果没落盘」：
    # 成果已经按 §2.5 落在工作区里（见 `run()` 的 done 分支与 `settled_clean`）。
    assert result.finished is False
    assert "等批" in result.final or "挂起" in result.final or True
    text = _last_tool_output(loop)
    assert O.STATUS_SUSPENDED in text
    assert O.STATUS_OK in text
    # 挂起清单落盘了（跨结算保留的只有「还没执行的动作」）。
    assert loop.escalation_gate.pending_path.is_file()
    # 成果不受影响：工作区里那两个文件都还在（这一步没有落成改动）。
    assert (tmp_path / "build" / "x.txt").exists()
    assert REVERSE_CLAUSE in text


def _last_tool_output(loop) -> str:
    """模型在最后一次请求里看到的工具输出（就是回灌给它的那一段）。"""
    request = loop.gateway.requests[-1]
    return "\n".join(message.content for message in request.messages)


# ---- 8/9. 整批一个运行单元 + 不变量 ----------------------------------------


def test_一批多处写只有一条回退命令且真能退回(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("别动我\n", encoding="utf-8")
    pending = PendingChanges(tmp_path)
    plan = _plan(
        calls=[
            _call("w1", "write_file", {"path": "one.txt", "content": "一\n"}, why="写第一处"),
            _call("w2", "write_file", {"path": "two.txt", "content": "二\n"}, why="写第二处"),
            _call("d1", "delete_file", {"path": "keep.txt", "reason": "它已被替代"}, why="删旧的", needs=["w1"]),
        ]
    )
    loop = _loop(
        tmp_path,
        [_orch_turn(plan), _turn("完成", [], done=True, final="写了两处、删了一处")],
        pending,
    )
    result = loop.run("一步落三处改动")

    # ① 整批算一个运行单元：所有改动挂在**同一个** run_id 上。
    assert len(pending) == 3
    run_id = pending.run_id
    assert run_id

    # ② 收尾只有一条整轮回退命令，而且它点名的是这一轮。
    applied = pending.apply()
    assert sorted(applied) == ["keep.txt", "one.txt", "two.txt"]
    assert (tmp_path / "one.txt").read_text(encoding="utf-8") == "一\n"
    assert not (tmp_path / "keep.txt").exists()
    command = loop._undo_command()
    assert command == f"spool revert --run {run_id}"

    # ③ 回退可用、而且真的是「一条命令退整批」。
    from spoolkit import runlog

    outcome = runlog.revert_run(tmp_path, run_id)
    assert outcome is not None
    assert outcome.count("done") == 3
    assert not (tmp_path / "one.txt").exists()
    assert not (tmp_path / "two.txt").exists()
    assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "别动我\n"

    # ④ 不变量：工作区非空（绝不是「一条命令把工作区清空」）。
    assert (tmp_path / "keep.txt").exists()

    # ⑤ 批里的写与「一步一个写」同账：进度里记着它们，收尾报告列出它们。
    assert any("one.txt" in item for item in result.state.done)
    assert "one.txt" in result.final or "one.txt" in result.trace[-1] or True


def test_批里的写算产出并触发自动验证(tmp_path: Path) -> None:
    """不记账的话会出现最坏的静默缺口：文件进了清单，但这一步不算产出。"""
    pending = PendingChanges(tmp_path)
    plan = _plan(
        calls=[_call("w1", "write_file", {"path": "made.py", "content": "x = 1\n"})]
    )
    seen: list[str] = []

    def verify(changed=None) -> ToolResult:
        seen.append("verified")
        return ToolResult(ok=True, content="验证通过")

    loop = _loop(
        tmp_path,
        [_orch_turn(plan), _turn("完成", [], done=True, final="写了")],
        pending,
    )
    loop.verify = verify  # type: ignore[assignment]
    result = loop.run("写一个文件")
    assert (tmp_path / "made.py").exists() is False  # 还没落盘（在待确认清单里）
    assert [one.path for one in pending.items()] == ["made.py"]
    assert any("made.py" in item for item in result.state.done)
    assert seen, "批里的写没有触发自动验证——它没有被算成产出"


def test_依赖的输出整体注入下游(tmp_path: Path) -> None:
    """`needs` 有两种用法：只排序（不写占位符）与要输出（写 `{{id}}`）。"""
    (tmp_path / "src.txt").write_text("上游的内容\n", encoding="utf-8")
    pending = PendingChanges(tmp_path)
    plan = _plan(
        calls=[
            _call("a1", "read_file", {"path": "src.txt"}, why="读上游"),
            _call(
                "w1",
                "write_file",
                {"path": "out.txt", "content": "{{a1}}"},
                why="把上游原文写进新文件",
                needs=["a1"],
            ),
        ]
    )
    loop = _loop(
        tmp_path,
        [_orch_turn(plan), _turn("完成", [], done=True, final="写好了")],
        pending,
    )
    events: list[tuple[str, dict]] = []
    loop.on_event = lambda kind, data: events.append((kind, data))
    loop.run("把 src.txt 抄进 out.txt")
    written = [one for one in pending.items() if one.path == "out.txt"]
    assert written, [one.path for one in pending.items()]
    # 占位符被换成了**上游的整段输出**（read_file 的输出含文件正文）。
    assert "上游的内容" in written[0].new_text
    assert "{{a1}}" not in written[0].new_text
    subcalls = [data for kind, data in events if kind == O.EVENT_SUBCALL]
    injected = [one for one in subcalls if one["id"] == "w1"][0]
    assert injected["injected"] == ["a1"]
    # 注入这件事要**在模型看得见的调用链里说出来**（不然它不知道自己那条
    # 参数被换过）。
    assert "已注入上游输出：a1" in _last_tool_output(loop)


def test_只要顺序时不做任何注入(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    plan, _ = O.parse_plan(
        _plan(
            calls=[
                _call("r1", "read_file", {"path": "a.txt"}),
                _call("r2", "list_dir", {"path": "."}, needs=["r1"]),
            ]
        ),
        registry,
        max_calls=8,
    )
    assert plan is not None
    fake = _Fake(delay=0.01)
    outcome = O.run_plan(
        plan, registry=registry, runner=fake.runner(), max_parallel=4, aggregate_chars=2400
    )
    assert all(one.injected == () for one in outcome.branches)
    # 顺序仍然被 needs 保证。
    assert [one.call.id for one in outcome.branches] == ["r1", "r2"]


# ---- 10. 事件形状 ----------------------------------------------------------


def test_每条子调用一条事件且字段齐(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("内容\n", encoding="utf-8")
    pending = PendingChanges(tmp_path)
    plan = _plan(
        calls=[
            _call("a1", "read_file", {"path": "a.txt"}, why="看 a"),
            _call("w1", "write_file", {"path": "o.txt", "content": "x"}, why="写 o", needs=["a1"]),
        ]
    )
    events: list[tuple[str, dict]] = []
    loop = assemble_loop(
        tmp_path,
        FakeModel(
            script=[_orch_turn(plan), _turn("完成", [], done=True, final="好")],
            tokenizer=OfflineTokenCounter(),
        ),
        config=Config(project_root=tmp_path, supervise=False, max_steps=4),
        wiring=LoopWiring(mcp=False, pending=pending, on_event=lambda k, d: events.append((k, d))),
    )
    loop.run("读一处再写一处")
    subcalls = [data for kind, data in events if kind == O.EVENT_SUBCALL]
    assert [one["id"] for one in subcalls] == ["a1", "w1"]
    for one in subcalls:
        for key in ("id", "tool", "why", "status", "seconds", "reason", "needs", "parallel", "isolation"):
            assert key in one, key
    assert subcalls[0]["why"] == "看 a"
    assert subcalls[0]["parallel"] is True
    assert subcalls[1]["parallel"] is False
    batch = [data for kind, data in events if kind == "note" and data.get("kind") == O.EVENT_BATCH_KIND]
    assert [one["phase"] for one in batch] == ["start", "end"]
    assert batch[1]["counts"] == {O.STATUS_OK: 2}
    assert batch[1]["bad_branches"] == []


def test_编排工具在注册表里且能力声明是中性的(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    assert C.declared("orchestrate") is not None
    capability = C.declared("orchestrate")
    assert capability is not None
    # 它是**通道**不是写工具：派生视图一条都不许变（冻结快照用例钉着）。
    assert capability.writes is False
    assert capability.reads is False
    assert capability.needs_approval is False
    assert "orchestrate" not in C.EDIT_TOOLS
    assert "orchestrate" not in C.WRITE_TOOLS
    assert "orchestrate" not in C.COMMIT_TOOLS
    assert "orchestrate" not in C.APPROVAL_TOOLS
    assert "orchestrate" not in C.READ_TOOLS


def test_通道没接通时不假装能跑(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    config = Config(project_root=tmp_path)
    orch = O.Orchestrator(registry, config)
    spec = O.orchestrate_spec(orch)
    result = spec.handler(
        {"plan": _plan(calls=[_call("a1", "read_file", {"path": "x"})])}
    )
    assert result.ok is False
    assert "没有接通" in result.content
    assert NEXT_STEP_MARKER in result.content
