"""编排工具（`orchestrate`）的反事实对照 —— **真模型**版。

上一轮（`counterfactual.py` + `readings.json`）用的是脚本化假 provider：两臂的
「脚本形状」本身就是自变量，步数与模型调用次数是**定义出来的**，不是模型自己
选的。这个脚本把同一件事换成真调本机 llama.cpp（`http://127.0.0.1:8080`），
于是「模型会不会写计划、写不写得对、用了之后到底省不省」变成**真读数**。

## 两个臂（唯一的差别是注册表里有没有那一件工具）

- `off`：**一次一调**。`orchestrate` **不注册**——装配完之后从注册表里把它拿掉
  （禁用开关，不是提示词里求它别用）。于是它既不在工具索引里，也不在 GBNF
  的工具名 enum 里，模型**看不见**它。
- `on`：**一步多调**。只是把 `orchestrate` 留着（`assemble_loop` 本来就会注册）。
  任务描述与提示词一个字都不加——不给它任何「你应该用编排」的暗示。

两个臂跑的是**同一段用户原话**（`GOALS`，逐字），同一份沙箱配方，同一套采样
参数，同一份判据。

## 判据（**跑之前就定好**，见 `CHECKS` 与 `checks_for`）

任务 A：① 覆盖预设的 6 条待办（按文件名逐个核）；② 有分类；③ 有优先级。
任务 B：① 点名了涉及的文件（fixture 自己声明的）；② 给出了修复计划。
机械判据，只认关键词，不看文采。

## 读数

逐次全量落 `real-readings.json`（不取最好那次）：步数 / 模型调用 / 输入 token /
输出 token / 墙钟 / 工具耗时 / 结果可用性 / 编排调用次数与合法性 / 计划原文 /
被拒原因 / 子调用状态。汇总（中位数 + 极差）写在同一个文件里。

跑法（本目录内可复现）：

```powershell
cd D:\\workSpace\\agents_dev
$env:ORCH_TOTAL_BUDGET = "3600"   # 整批墙钟上限（秒），有界；快撞线时**减设置不减重复**
$env:ORCH_REAL_SETTINGS = "0"     # 逗号分隔的采样温度，例如 "0,0.4"
.venv\\Scripts\\python.exe benchmarks\\probes\\orchestration\\real_counterfactual.py
```
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
STATE_FILE = Path(__file__).resolve().parent / ".real-state.json"
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(OUT))

#: Windows 控制台是 cp936：中文与 `�` 都会让 print 抛 UnicodeEncodeError，
#: 而这次对照的读数全是中文。**先保住日志**：能重设就重设成 UTF-8。
try:  # pragma: no cover - 只影响终端那条路
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

LOG_PATH = OUT / "real-run-log.txt"


def say(text: str) -> None:
    """打印一行，同时进日志。**两边都要有**：终端给人看，日志给报告看。"""
    print(text)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(text + "\n")


# ===========================================================================
# 阶段 0：沙箱配方（与阶段 1 分开进程跑）
# ===========================================================================

#: 沙箱里预置的待办标记。这个仓库里几乎没有 `TODO` 字面量（`rg "TODO|FIXME|XXX"
#: src/spoolkit` 只有 1 条误命中），按原样跑任务 A 会量到「没东西可取」而不是
#: 「取数 + 归纳」。所以按同一份配方预置 6 条——**两个臂拿到逐字相同的沙箱**。
#: 这条在上一轮的报告里已经写明，这里沿用（`SEED_MARK` / `SEED_FILES`）。
from counterfactual import SEED_FILES, SEED_MARK  # noqa: E402  (同目录)

#: task B 的失败测试 fixture。为什么自己造一个而不是沿用上一轮的
#: `tests/cli/test_service_command.py`：那个文件在**本机沙箱里是红的**
#: （taskkill 被拦），红的原因不是代码而是环境，模型给出的「修复计划」会指向
#: 沙箱而不是缺陷。这里造一个**确定性失败**：一行代码少了 `stopped = True`，
#: pytest 断言红，失败信息指向 `services.py` 的那一行。
FIXTURE_SERVICE = '''"""本机服务配方：拉起 / 停掉 / 按 pid 记录判状态。"""

from __future__ import annotations

import json
from pathlib import Path

STATE = Path(__file__).resolve().parent / "state.json"
PID_FILE = STATE.parent / "service.pid"


class Service:
    def __init__(self, name: str) -> None:
        self.name = name
        self.running = False
        self.pid: int | None = None
        self.stopped = False


def _read_record() -> dict:
    if not PID_FILE.is_file():
        return {}
    return json.loads(PID_FILE.read_text(encoding="utf-8"))


def _write_record(payload: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(json.dumps(payload), encoding="utf-8")


def start_service(name: str) -> tuple[int, str]:
    """拉起一个配方。已经有活着的 pid 记录就不重复拉起（判据是记录，不是探针）。"""
    record = _read_record()
    if record.get("name") == name and record.get("alive"):
        return 0, f"{name} 已经在跑（pid {record['pid']}）"
    _write_record({"name": name, "pid": 4242, "alive": True})
    return 0, f"{name} 已拉起（pid 4242）"


def stop_service(service: Service) -> tuple[int, str]:
    """停掉一个配方：写停止记录，并清掉活着的 pid 记录。

    配方自己退出之后，pid 记录里必须留下「已停止」，否则下一次 start 会照着
    一条过期记录说「已经在跑」，用户以为自己有服务、其实一个进程都没有。
    """
    if not service.running and not PID_FILE.is_file():
        return 2, f"没有本命令启动的 {service.name}"
    _write_record({"name": service.name, "pid": service.pid, "alive": False})
    # BUG：这里少了 service.stopped = True
    return 0, f"{service.name} 已停止"
'''

FIXTURE_TEST = '''"""`stop_service` 的行为：判据是 pid 记录，不是内存里的那个布尔值。"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import spoolkit.services as svc  # noqa: E402
from spoolkit.services import Service, _read_record, stop_service  # noqa: E402


def _fresh_workdir(name: str) -> Path:
    """本用例自己的现场目录（不用 pytest 的 tmp_path，免得跟平台临时根纠缠）。"""
    base = Path(__file__).resolve().parent / f"_work_{name}"
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    return base


def test_停下之后状态与pid记录都要对上(monkeypatch) -> None:
    workdir = _fresh_workdir("stop")
    monkeypatch.setattr(svc, "PID_FILE", workdir / "service.pid")
    monkeypatch.setattr(svc, "STATE", workdir / "state.json")
    (workdir / "service.pid").write_text(
        json.dumps({"name": "tunnel", "pid": 4242, "alive": True}), encoding="utf-8"
    )
    service = Service("tunnel")
    service.pid = 4242

    code, message = stop_service(service)

    assert code == 0, message
    assert service.stopped is True, "停下之后 service.stopped 必须为真"
    record = _read_record()
    assert record["alive"] is False, f"pid 记录里的 alive 要为假：{record}"
'''


def sandbox_recipe() -> dict:
    """沙箱配方：复制什么、预置什么。两个臂逐字相同。"""
    return {
        "copy": (
            ("src/spoolkit", "src/spoolkit"),
            ("tests", "tests"),
            ("pyproject.toml", "pyproject.toml"),
        ),
        "seed_todos": SEED_FILES,
        "seed_mark": SEED_MARK,
        "write": (
            ("src/spoolkit/services.py", FIXTURE_SERVICE),
            ("tests/services/test_services.py", FIXTURE_TEST),
        ),
    }


def _build_sandbox(project: Path) -> str:
    """造一个沙箱副本。**绝不在仓库里跑**，也不留下一份 `.agent`。"""
    ignore = shutil.ignore_patterns(
        "__pycache__",
        ".pytest_cache",
        ".agent",
        ".git",
        "*.pyc",
        ".venv",
        # 仓库自己的 `tests/conftest.py` 有一条 autouse fixture 会往平台的
        # `pytest-of-<user>` 根建临时目录；受限沙箱下那个根建不出来，于是
        # **每一条**测试都在 setup 阶段 ERROR——失败原因就不是代码而是环境了。
        # 这个副本是给模型看的现场，不是要跑全套回归，所以不抄 conftest。
        "conftest.py",
    )
    recipe = sandbox_recipe()
    for source_rel, target_rel in recipe["copy"]:
        source = REPO / source_rel
        target = project / target_rel
        if source.is_dir():
            shutil.copytree(source, target, ignore=ignore)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    for relative, text in recipe["write"]:
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    seeded = 0
    for index, relative in enumerate(recipe["seed_todos"]):
        target = project / relative
        if not target.is_file():
            continue
        with target.open("a", encoding="utf-8") as handle:
            handle.write(
                f"\n# {recipe['seed_mark']}: 第 {index + 1} 处待办——把这一段抽成独立函数\n"
            )
        seeded += 1
    # 上一轮（第一次冒烟）留下的 pytest 缓存目录会被 `list_dir` 看见，而它们
    # 是**现场噪声**：模型会以为自己该看一眼。清掉。
    for junk in project.glob("pytest-cache-files-*"):
        shutil.rmtree(junk, ignore_errors=True)
    return f"预置 {seeded}/{len(recipe['seed_todos'])} 条待办"


def stage_sandbox() -> int:
    """阶段 0：造沙箱、跑一遍失败测试确认它**真的红**，然后把路径留给阶段 1。"""
    from spoolkit import sandbox_dirs

    base = Path(sandbox_dirs.make_accessible_temp_dir("spool-orch-real-"))
    project = base / "project"
    note = _build_sandbox(project)
    python = REPO / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        python = Path(sys.executable)
    started = time.perf_counter()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    finished = subprocess.run(
        [
            str(python),
            "-m",
            "pytest",
            "-q",
            # 沙箱里建不出 `.pytest_cache`（受限目录），关掉缓存插件免得噪声
            # 盖住真正的失败原文。用例本身不用 tmp_path，所以也不碰平台临时根。
            "-p",
            "no:cacheprovider",
            "tests/services/test_services.py",
        ],
        cwd=str(project),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    seconds = time.perf_counter() - started
    # 失败原文按 utf-8 落一份：终端是 cp936，中文与替换字符会把 print 弄炸，
    # 而「这次真的红了」的证据必须在。日志走 `errors="replace"`。
    raw = (finished.stdout or "") + (finished.stderr or "")
    (OUT / "real-sandbox-pytest.txt").write_text(raw, encoding="utf-8")
    tail = "\n".join((finished.stdout or "").strip().splitlines()[-12:])
    say(f"沙箱 : {project}")
    say(f"配方 : {note}")
    say(f"失败测试：exit={finished.returncode}（{seconds:.1f}s）")
    say(tail)
    STATE_FILE.write_text(
        json.dumps(
            {
                "project": str(project),
                "todo_seed": len(SEED_FILES),
                "fail_exit": finished.returncode,
                "fail_tail": tail,
                "fail_seconds": round(seconds, 2),
                "cleanup": "跑完用 `python <本脚本> clean` 删掉这个沙箱",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return 0


def stage_clean() -> int:
    """删掉阶段 0 造的那个沙箱（它自己带 `project` 这一层，只删这一层）。"""
    if not STATE_FILE.is_file():
        print("没有 .real-state.json，没什么可删的")
        return 0
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    base = Path(state["project"]).resolve().parent
    if base.name == "project" or "spool-orch-real-" not in base.name:
        print(f"拒绝删除（路径不像我们造的沙箱）：{base}")
        return 2
    shutil.rmtree(base, ignore_errors=True)
    print(f"已删 {base}（还在：{base.exists()}）")
    return 0


# ===========================================================================
# 阶段 1：真模型对照
# ===========================================================================

#: **用户原话形状，一个字不改**。两个臂逐字相同。
GOALS = {
    "A-todo": "把这个仓库里的 TODO 汇总起来，分类，给出优先级。",
    "B-failing-test": "找出这个失败测试涉及哪些文件，并给出修复计划。",
}

#: 任务 A 的覆盖面判据：6 条预置待办分别在哪个文件里。
CHECK_FILES = tuple(one.split("/")[-1] for one in SEED_FILES)
#: 分类 / 优先级的机械判据。**跑之前定好**，只认这些记号，不靠事后判断。
CHECK_CLASSIFY = ("分类", "类别", "归类")
CHECK_PRIORITY = ("优先级", "优先", "P0", "P1", "P2", "P3", "高", "中", "低")
#: 任务 B 的判据：涉及的文件名（fixture 自己声明的）+ 修复计划的记号。
CHECK_B_FILES = ("services.py", "test_services.py")
CHECK_B_PLAN = ("修复", "计划", "步骤", "建议", "改成", "应当", "应该", "需要")

CHECKS = {
    "A": ("覆盖预设待办（6/6）", "有分类", "有优先级"),
    "B": ("点名涉及的文件", "给出修复计划"),
}


def checks_for(task: str, final: str) -> dict[str, bool]:
    """机械判据。**先定好再跑**，不看措辞好坏。"""
    text = final or ""
    if task == "A-todo":
        hits = [one for one in CHECK_FILES if one in text]
        return {
            CHECKS["A"][0]: len(hits) == len(CHECK_FILES),
            CHECKS["A"][1]: any(one in text for one in CHECK_CLASSIFY),
            CHECKS["A"][2]: any(one in text for one in CHECK_PRIORITY),
            "_hits": hits,
        }
    return {
        CHECKS["B"][0]: all(one in text for one in CHECK_B_FILES),
        CHECKS["B"][1]: any(one in text for one in CHECK_B_PLAN),
    }


# ---- 装配现场（每次跑都要记）-----------------------------------------------


def scene_now() -> dict:
    """这次跑所处的版本与装配现场：HEAD / 工具件数 / system 区段 token。"""
    from spoolkit.config import Config
    from spoolkit.llm.tokenizer import OfflineTokenCounter
    from spoolkit.cli.runtime import assemble_loop

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(REPO), capture_output=True, text=True
    ).stdout.strip()
    # 装配现场一律按**仓库自身**量（沙箱里没有 pyproject 之外的装配差异；
    # 这里要读的是「另一个写者现在把提示词/索引改成了什么样」）。
    project = Path(os.environ.get("ORCH_SCENE_ROOT", str(REPO)))
    loop = assemble_loop(
        project,
        _NullGateway(),
        config=Config(project_root=project, supervise=False, context_window=8192),
        wiring=_scene_wiring(project),
    )
    registry = loop.registry
    system_text = _system_text(loop)
    counter = OfflineTokenCounter()
    return {
        "head": head,
        "tool_count": len(registry.names()),
        "has_orchestrate": registry.get("orchestrate") is not None,
        "system_chars": len(system_text),
        "system_tokens": counter.count(system_text),
        "tools": list(registry.names()),
    }


def _scene_wiring(project: Path):
    from spoolkit.cli.runtime import LoopWiring
    from spoolkit.tools.edit import PendingChanges

    return LoopWiring(mcp=False, pending=PendingChanges(project))


def _system_text(loop) -> str:
    """精装那份 system 提示词（与 `AgentLoop._assemble` 逐字同一个调用）。"""
    from spoolkit.agent.loop import SYSTEM_PROMPT
    from spoolkit.agent.loop import build_workflow

    return SYSTEM_PROMPT.format(
        tools=loop.registry.describe(),
        workflow=build_workflow(loop.registry, (), from_channel=False),
    )


class _NullGateway:
    """只为了装配现场而存在的空网关：不生成、不联网。"""

    def context_window(self) -> int | None:
        return 8192

    def chat(self, request):  # pragma: no cover - 装配不会调它
        raise RuntimeError("空网关不该被调用")


# ---- 真模型跑一次 -----------------------------------------------------------


def run_once(task: str, arm: str, temperature: float, label: str) -> dict:
    from spoolkit.cli.runtime import LoopWiring, assemble_loop, counter_for
    from spoolkit.config import Config
    from spoolkit.llm.llamacpp import LlamaCppGateway
    from spoolkit.tools.edit import PendingChanges

    # 每一次都记下**这一次跑的是哪个版本**。另一个写者会动工具索引/提示词，
    # 读数必须能按版本解释，不能笼统说「HEAD 是 X」。
    head = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=str(REPO), capture_output=True, text=True
    ).stdout.strip()
    project = Path(json.loads(STATE_FILE.read_text(encoding="utf-8"))["project"])
    gateway = LlamaCppGateway(
        base_url=os.environ.get("ORCH_BASE_URL", "http://127.0.0.1:8080"),
        model=os.environ.get("ORCH_MODEL", "local"),
        timeout=float(os.environ.get("ORCH_HTTP_TIMEOUT", "600")),
        temperature=temperature,
        seed=None if temperature == 0 else int(os.environ.get("ORCH_SEED", "7")),
    )
    tokenizer = counter_for(gateway)
    pending = PendingChanges(project)
    events: list[tuple[str, dict]] = []
    #: 每一次 `orchestrate` 调用的「模型写了什么 + 系统回了什么」。事件流不足以
    #: 拿到这两样（`tool` 事件的 payload 里没有 arguments，detail 又被截到
    #: 200 字），而这个对照的**首要问题**正是「计划写出来了吗、合法吗」——
    #: 所以在这一层绕一下处理器，**只读地记一份**（不改工具实现、不改提示词）。
    plan_calls: list[dict] = []
    #: 逐回合的「模型自己一步发了几个调用」。主循环的 `tool_calls` 读数包含
    #: 自动验证/最终验证那几条**循环自己发的**，分不出「模型一步多调」这件事，
    #: 所以额外记一份：每一回合的 tool_calls 条数。
    turns: list[dict] = []
    _instrument_gateway(gateway, turns)

    def on_event(kind: str, data: dict) -> None:
        events.append((kind, data))

    wiring_started = time.perf_counter()
    loop = assemble_loop(
        project,
        gateway,
        config=Config(
            project_root=project,
            supervise=False,
            max_steps=int(os.environ.get("ORCH_MAX_STEPS", "12")),
            context_window=8192,
        ),
        wiring=LoopWiring(mcp=False, pending=pending, on_event=on_event),
    )
    wiring_seconds = time.perf_counter() - wiring_started
    if arm == "on":
        _tap_orchestrator(loop, plan_calls)
    if arm == "off":
        # **禁用开关**：装配完之后把 `orchestrate` 从注册表里拿掉。它已按单调用
        # 路径注册；这里只做「摘除」这一件事，不改任何工具实现，也不碰提示词。
        removed = loop.registry._specs.pop("orchestrate", None)
        assert removed is not None, "off 臂：注册表里没有 orchestrate 可摘"
    if arm == "control":
        # **安慰剂臂**：拿掉 `orchestrate`，塞一件**名字与用途都不是编排**的占位
        # 工具，但把索引里那一行的**长度调到逐字相同**。于是它与 `on` 臂在
        # 「工具件数」「system 区段大小」上完全对齐，只差那一个名字/用途。
        # 没有这一臂就分不清「工具在册带来的收益」与「模型看到的东西不同导致的
        # 轨迹分叉」——而这次的读数恰好落在那个歧义上。
        removed = loop.registry._specs.pop("orchestrate", None)
        assert removed is not None, "control 臂：注册表里没有 orchestrate 可摘"
        loop.registry.register(_sham_spec(removed))
    tool_count = len(loop.registry.names())
    system_tokens = tokenizer.count(_system_text(loop))

    started = time.perf_counter()
    result = loop.run(GOALS[task])
    wall = time.perf_counter() - started
    gateway.close()

    # ---- 从事件流里把编排这件事逐条读出来 ---------------------------------
    orch_calls: list[dict] = []
    subcalls: list[dict] = []
    tools: list[dict] = []
    for kind, data in events:
        if kind == "tool":
            tools.append(data)
            if data.get("name") == "orchestrate":
                orch_calls.append(data)
        elif kind == "subcall":
            subcalls.append(data)

    runner = _plan_evidence(plan_calls)
    plans = []
    for index, one in enumerate(orch_calls):
        plans.append(
            {
                "index": index,
                "ok": bool(one.get("ok")),
                "detail": one.get("detail", ""),
            }
        )
    rejected = [one for one in plans if not one["ok"]]
    tap_ok = (not orch_calls) or (len(plan_calls) >= len(orch_calls))
    return {
        "task": task,
        "arm": arm,
        "temperature": temperature,
        "label": label,
        "head": head,
        "started_at": time.strftime("%H:%M:%S"),
        "goal": GOALS[task],
        "steps": result.steps,
        "model_calls": result.model_calls,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "wall_seconds": round(wall, 3),
        "model_seconds": round(result.model_seconds, 3),
        "tool_seconds": round(result.tool_seconds, 3),
        "wiring_seconds": round(wiring_seconds, 3),
        "resets": result.resets,
        "finished": result.finished,
        "final": result.final,
        "tool_calls": len(tools),
        "tool_names": [one.get("name") for one in tools],
        "model_turns": turns,
        "model_turns_count": len(turns),
        "model_multi_call_turns": sum(1 for one in turns if one["tool_calls"] > 1),
        "model_max_calls_in_turn": max([one["tool_calls"] for one in turns] or [0]),
        "model_calls_in_turns": sum(one["tool_calls"] for one in turns),
        "orchestrate_calls": len(orch_calls),
        "orchestrate_plans_ok": len(plans) - len(rejected),
        "orchestrate_plans_rejected": len(rejected),
        "orchestrate_rejects": [one["detail"] for one in rejected],
        "orchestrate_plan_texts": [one.get("plan_text", "") for one in runner],
        "orchestrate_full_rejects": [one["content"] for one in runner if not one["ok"]],
        "tap_ok": tap_ok,
        "orchestrate_plan_shape": [
            {"ok": one["ok"], "why": one["why"], "calls": one["calls"],
             "aggregate": one["aggregate"], "batch_edits": one["batch_edits"]}
            for one in runner
        ],
        "orchestrate_batch_edits": sum(one["batch_edits"] for one in runner),
        "subcalls": len(subcalls),
        "subcall_status": _tally(one.get("status", "?") for one in subcalls),
        "subcall_parallel": sum(1 for one in subcalls if one.get("parallel")),
        "subcall_serial": sum(1 for one in subcalls if not one.get("parallel")),
        "subcall_tools": sorted({str(one.get("tool")) for one in subcalls}),
        "tool_count": tool_count,
        "system_tokens": system_tokens,
        "max_steps": int(os.environ.get("ORCH_MAX_STEPS", "12")),
        "checks": checks_for(task, result.final),
    }


def _tally(items) -> dict:
    out: dict[str, int] = {}
    for one in items:
        key = str(one)
        out[key] = out.get(key, 0) + 1
    return out


# ---- 计划原文（成功 / 失败各留一份）----------------------------------------


def _tap_orchestrator(loop, sink: list[dict]) -> None:
    registry = loop.registry
    original = registry.invoke

    def tapped(call):
        if call.name != "orchestrate":
            return original(call)
        result = original(call)
        sink.append(
            {
                "plan": dict(call.arguments).get("plan"),
                "ok": bool(getattr(result, "ok", False)),
                "content": getattr(result, "content", ""),
            }
        )
        return result

    registry.invoke = tapped


def _isolate_local_http() -> None:
    """本机 8080 不走代理。

    **实测踩到的坑**：`spoolkit.llm.llamacpp.LlamaCppTokenCounter` 建 httpx 客户端时
    只传了 `proxy=`，没关 `trust_env`；环境里一旦出现 `HTTP_PROXY=http://127.0.0.1:7890`
    （Clash 那条），它就会把 `/tokenize` 也送去代理，代理对 `127.0.0.1` 的目标回
    **502**——整批跑从那一刻起全部「未完成（tokenize 返回 502）」。这不是服务挂了
    （`/props` 与 `/v1/chat/completions` 都正常），是**本机回环被代理劫走**。
    这里把这次运行的环境清干净：跑分器不改仓库代码，只保证自己这一次的环境是对的，
    并把这件事写进读数（`local_http_isolated`）。
    """
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        os.environ.pop(name, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost,::1"
    os.environ["no_proxy"] = "127.0.0.1,localhost,::1"


def _sham_spec(original) -> object:
    """安慰剂工具：索引行长度与 `original` **逐字相同**，行为是「什么也不做」。

    判据是**索引里那一行**（`registry.describe` 的输出），不是 description——
    进 system 区段的只有那一行（`name(params) brief`）。所以先量出原行有多长，
    再用同样的字数写一句与编排无关的话；参数名照抄（`name(params)` 那一段）。
    """
    from spoolkit.tools.registry import _index_entry
    from spoolkit.tools.types import ToolResult, ToolSpec

    line = _index_entry(original)
    head = line.split(" ", 1)[0]
    brief = line[len(head) + 1 :] if " " in line else ""
    spec = ToolSpec(
        name="noop_tool",
        description="占位工具，不做任何事",
        parameters=original.parameters,
        handler=lambda args: ToolResult(ok=True, content="（占位工具，没有做事）"),
        brief="占位工具，不做任何事",
        group=original.group,
    )
    short = len(line) - len(_index_entry(spec))
    if short > 0:
        spec = ToolSpec(
            name=spec.name,
            description=spec.description,
            parameters=spec.parameters,
            handler=spec.handler,
            brief=spec.brief + "·" * short,
            group=spec.group,
        )
    assert len(_index_entry(spec)) == len(line), (
        f"安慰剂行的长度必须与原来那一行逐字相同："
        f"{len(_index_entry(spec))} != {len(line)}（原 brief={brief!r}）"
    )
    return spec


def _instrument_gateway(gateway, turns: list[dict]) -> None:
    """记下**模型每一个回合自己发了几个工具调用**（以及是不是 done）。

    主循环的 `tool_calls` 读数里混着自动验证 / 最终验证那几条**循环自己发的**，
    分不出「模型一步多调」这件事。这一步只读，不改任何行为。
    """
    original = gateway.chat

    def tapped(request):
        response = original(request)
        try:
            payload = json.loads(response.text)
        except Exception:
            payload = None
        calls = (payload or {}).get("tool_calls")
        calls = calls if isinstance(calls, list) else []
        turns.append(
            {
                "tool_calls": len(calls),
                "names": [item.get("name") for item in calls if isinstance(item, dict)],
                "done": bool((payload or {}).get("done")),
                "chars": len(response.text or ""),
            }
        )
        return response

    gateway.chat = tapped


def _plan_evidence(plan_calls: list[dict]) -> list[dict]:
    """把每一次 `orchestrate` 调用整理成一条证据：**模型写的那份计划原文**。

    计划正文来自调用**当时**的 `args["plan"]`（不是事后的复述），拒绝话术来自
    工具真正返回的那份 `content`（不是事件流里被截到 200 字的 `detail`）。
    """
    out: list[dict] = []
    for one in plan_calls:
        plan = one.get("plan") or {}
        calls = plan.get("calls") if isinstance(plan, dict) else None
        items = [item for item in (calls if isinstance(calls, list) else []) if isinstance(item, dict)]
        tools = [item.get("tool") for item in items]
        out.append(
            {
                "ok": bool(one.get("ok")),
                "plan_text": json.dumps(plan, ensure_ascii=False),
                "content": one.get("content", ""),
                "why": plan.get("why", "") if isinstance(plan, dict) else "",
                "aggregate": plan.get("aggregate", "") if isinstance(plan, dict) else "",
                "calls": [
                    {
                        "id": item.get("id"),
                        "tool": item.get("tool"),
                        "args": item.get("args"),
                        "why": item.get("why"),
                        "needs": item.get("needs"),
                    }
                    for item in items
                ],
                # 「把不该并行的写操作塞进一批」这件事要能被数出来：计划里出现
                # 会改工作区的工具就记一笔（它们会被串行执行，但仍是「一批里
                # 混了写」的证据）。
                "batch_edits": sum(
                    1 for name in tools if name in ("write_file", "replace_text", "edit", "apply_patch")
                ),
            }
        )
    return out


# ===========================================================================
# 主流程
# ===========================================================================


def _settings() -> list[float]:
    raw = os.environ.get("ORCH_REAL_SETTINGS", "0")
    return [float(one) for one in raw.split(",") if one.strip()]


def _tasks() -> dict[str, str]:
    """要跑哪几个任务（`ORCH_TASKS` 逗号分隔的键；不给就是全部）。"""
    raw = os.environ.get("ORCH_TASKS", "")
    if not raw.strip():
        return dict(GOALS)
    return {key: GOALS[key] for key in GOALS if key in {one.strip() for one in raw.split(",")}}


def _repeats() -> int:
    return int(os.environ.get("ORCH_REPEATS", "3"))


def _budget() -> int:
    return int(os.environ.get("ORCH_TOTAL_BUDGET", "3600"))


#: 已经落进 `real-readings.json` 的那些格子的标签。补跑（例如后加的安慰剂臂）
#: 时不该把已经花掉的格子再买一遍——那是白花钱，也会把读数搞成两套设置。
DONE_FILE = OUT / "real-done.json"


def _done_labels() -> set[str]:
    if not os.environ.get("ORCH_SKIP_DONE"):
        return set()
    try:
        return set(json.loads(DONE_FILE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _write_done(readings: list[dict]) -> None:
    labels = sorted({one["label"] for one in readings})
    if os.environ.get("ORCH_SKIP_DONE"):
        old = _done_labels()
        labels = sorted(old | set(labels))
    DONE_FILE.write_text(json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8")


def _stage1() -> int:
    _isolate_local_http()
    tasks = _tasks()
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    budget = _budget()
    settings = _settings()
    repeats = _repeats()
    scene = scene_now()
    say(f"\n===== 真模型对照 @ {time.strftime('%Y-%m-%d %H:%M:%S')} =====")
    say(f"版本 : HEAD {scene['head'][:12]} 工具 {scene['tool_count']} 件 "
        f"system {scene['system_tokens']} token（off 臂装配后摘掉 orchestrate）")
    say(f"预算 : {budget}s；设置 {settings}；任务 {list(tasks)}；每格 {repeats} 次")
    say(f"本机回环隔离代理：NO_PROXY={os.environ.get('NO_PROXY')}")
    say(f"沙箱 : {state['project']}（失败测试 exit={state['fail_exit']}）")

    # 次序：设置 → 臂 → 任务 → 重复。**只跑还没跑过的格子**（`real-done.json`
    # 记着已经落进 readings 的标签），补跑安慰剂臂时不会把已经花掉的格子再买一遍。
    # 设置放外层，是为了「预算不够时**减设置不减重复**」。
    done = _done_labels()
    readings: list[dict] = []
    skipped: list[dict] = []
    started = time.time()
    for temperature in settings:
        for arm in ("off", "on", "control"):
            for task in tasks:
                for repeat in range(repeats):
                    label = f"{task}|t{temperature}|{arm}|r{repeat}"
                    if label in done:
                        say(f"{label}: 已有读数，跳过")
                        continue
                    elapsed = time.time() - started
                    used = elapsed / budget if budget else 0.0
                    if budget and elapsed >= budget:
                        skipped.append({"label": label, "reason": "整批预算已用尽"})
                        say(f"{label}: 跳过（整批预算已用尽 {elapsed:.0f}s / {budget}s）")
                        continue
                    say(f"{label}: 开跑（已用 {elapsed:.0f}s / {budget}s）…")
                    try:
                        record = run_once(task, arm, temperature, label)
                    except Exception as exc:  # noqa: BLE001 - 一次失败不许带走整批
                        skipped.append(
                            {"label": label, "reason": f"{type(exc).__name__}: {exc}"}
                        )
                        say(f"{label}: 未完成（{type(exc).__name__}: {exc}）")
                        continue
                    record["elapsed_before"] = round(elapsed, 1)
                    record["budget_used"] = round(used, 3)
                    readings.append(record)
                    passed = [key for key, value in record["checks"].items()
                              if key != "_hits" and value]
                    say(
                        f"  步数 {record['steps']:2d} 模型调用 {record['model_calls']:2d} "
                        f"输入 {record['prompt_tokens']:6d} 输出 {record['completion_tokens']:4d} "
                        f"墙钟 {record['wall_seconds']:6.1f}s 工具 {record['tool_seconds']:5.1f}s "
                        f"工具调用 {record['tool_calls']:2d} 编排 {record['orchestrate_calls']} "
                        f"（合法 {record['orchestrate_plans_ok']}/拒 {record['orchestrate_plans_rejected']}）"
                        f" 子调用 {record['subcalls']} 可用 {len(passed)} 项 "
                        f"多调回合 {record['model_multi_call_turns']}/"
                        f"{record['model_turns_count']} "
                        f"{'完成' if record['finished'] else '未完成'}"
                    )
                    if record["orchestrate_rejects"]:
                        say("  拒绝原因：")
                        for one in record["orchestrate_full_rejects"][0].splitlines()[:6]:
                            say(f"    {one}")
                    if record["arm"] == "on":
                        for one in record["orchestrate_plan_shape"]:
                            mark = "合法" if one["ok"] else "被拒"
                            say(
                                f"  计划（{mark}）：{len(one['calls'])} 条 "
                                f"why={one['why'][:60]!r} "
                                f"写操作 {one['batch_edits']} 条"
                            )
                    _flush(readings, skipped, settings, repeats, budget, scene, started)
    _flush(readings, skipped, settings, repeats, budget, scene, started)
    say("===== 跑完 =====")
    return 0


def _summary(readings: list[dict], settings: list[float]) -> dict:
    """中位数 + 极差，逐设置逐臂。**全量读数都在 readings 里，不取最好那次。**"""
    out: dict[str, dict] = {}
    keys = (
        "steps",
        "model_calls",
        "prompt_tokens",
        "completion_tokens",
        "wall_seconds",
        "model_seconds",
        "tool_seconds",
        "tool_calls",
        "orchestrate_calls",
        "subcalls",
        "model_multi_call_turns",
        "model_max_calls_in_turn",
    )
    for task in [one for one in GOALS if any(r["task"] == one for r in readings)]:
        for temperature in sorted({one["temperature"] for one in readings}):
            for arm in ("off", "on", "control"):
                rows = [
                    one
                    for one in readings
                    if one["task"] == task
                    and one["arm"] == arm
                    and one["temperature"] == temperature
                ]
                if not rows:
                    continue
                cell: dict = {"runs": len(rows), "finished": sum(1 for one in rows if one["finished"])}
                for key in keys:
                    values = [one[key] for one in rows]
                    cell[key] = {
                        "median": statistics.median(values),
                        "min": min(values),
                        "max": max(values),
                    }
                checks = [one["checks"] for one in rows]
                cell["checks"] = {
                    key: {
                        "pass": sum(1 for one in checks if one.get(key)),
                        "of": len(checks),
                    }
                    for key in checks[0]
                    if key != "_hits"
                }
                cell["orchestrate_plans_ok"] = sum(one["orchestrate_plans_ok"] for one in rows)
                cell["orchestrate_plans_rejected"] = sum(
                    one["orchestrate_plans_rejected"] for one in rows
                )
                cell["orchestrate_used_runs"] = sum(
                    1 for one in rows if one["orchestrate_calls"] > 0
                )
                out[f"{task}/t{temperature}/{arm}"] = cell
    return out


def _flush(readings, skipped, settings, repeats, budget, scene, started) -> None:
    target = OUT / "real-readings.json"
    # 补跑时把上一批的读数原样并回来：这一批只买了几个新格子，汇总必须覆盖
    # **全部**读数，否则报告会把自己上一批的证据丢掉。
    previous: list[dict] = []
    if os.environ.get("ORCH_SKIP_DONE") and target.is_file():
        try:
            previous = json.loads(target.read_text(encoding="utf-8")).get("readings", [])
        except Exception:
            previous = []
    known = {one["label"] for one in readings}
    merged = previous + [one for one in readings if one["label"] not in
                         {p["label"] for p in previous}]
    payload = {
        "kind": "real-model counterfactual",
        "provider": os.environ.get("ORCH_BASE_URL", "http://127.0.0.1:8080"),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "budget_seconds": budget,
        "settings": sorted({one["temperature"] for one in merged}),
        "repeats": repeats,
        "scene": scene,
        "scene_after": _scene_light(),
        "sandbox": json.loads(STATE_FILE.read_text(encoding="utf-8")),
        "goals": GOALS,
        "local_http_isolated": True,
        "no_proxy": os.environ.get("NO_PROXY"),
        "checks_definition": {
            "A": list(CHECKS["A"]),
            "B": list(CHECKS["B"]),
            "classify_markers": list(CHECK_CLASSIFY),
            "priority_markers": list(CHECK_PRIORITY),
            "b_files": list(CHECK_B_FILES),
            "b_plan_markers": list(CHECK_B_PLAN),
        },
        "readings": merged,
        "skipped": skipped,
        "summary": _summary(merged, sorted({one["temperature"] for one in merged})),
        "elapsed_seconds": round(time.time() - started, 1),
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_done(merged)


def _scene_light() -> dict:
    """跑完之后再看一眼 HEAD（另一个写者可能在跑的过程中改了代码）。"""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(REPO), capture_output=True, text=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", "src"],
            cwd=str(REPO),
            capture_output=True,
            text=True,
        ).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    return {"head": head, "src_dirty": dirty}


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else "run"
    if action == "sandbox":
        return stage_sandbox()
    if action == "clean":
        return stage_clean()
    if action == "scene":
        print(json.dumps(scene_now(), ensure_ascii=False, indent=2))
        return 0
    return _stage1()


if __name__ == "__main__":
    raise SystemExit(main())
