"""S2（O2）：真机对照的**靶角色**与**靶判词**。

为什么要有这个文件：`benchmarks/probes/orchestration/real_counterfactual.py`
跑了三批真模型对照，但「A 靶没有区分度」这个结论一直是**人肉从读数里看出来的**——
代码里没有一处地方写着 A 是控制靶，也没有任何机械判据在跑完之后说「这个靶量不出
差别」。于是 A 靶的 0/3（其实是子进程 PATH 里没有 rg）被记成了「模型不行」，一路
写进报告。这里把两件事钉住：

1. 每个靶**必须有角色**，控制靶的判据反转为「各臂都应通过，哪格不过先查环境」；
2. 能力靶**必须有区分度**才算合格（两臂全过／全不过都判 `no_discrimination`）。

用例只跑纯函数与一次落盘，不调用模型、不起子进程跑对照。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from functools import lru_cache
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HARNESS_DIR = REPO / "benchmarks" / "probes" / "orchestration"
HARNESS = HARNESS_DIR / "real_counterfactual.py"


@lru_cache(maxsize=1)
def _harness():
    """把跑分脚本当模块加载。它自己会把 `REPO/src` 与所在目录挂进 `sys.path`。"""
    for one in (str(HARNESS_DIR), str(REPO / "src")):
        if one not in sys.path:
            sys.path.insert(0, one)
    spec = importlib.util.spec_from_file_location("real_counterfactual_probe", HARNESS)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _isolate_probe_log(tmp_path, monkeypatch):
    """单元测试不许往仓库里的探针日志追加内容。

    `remove_sandbox()` 的拒绝话术走 `say()`，而 `say()` 写的是模块级
    `LOG_PATH`（`benchmarks/probes/orchestration/real-run-log.txt`）——跑一次
    用例就让 `git status` 多出一行脏改动，混进提交前的路径清单里。把它指到
    临时目录；探针自己真被跑起来时不受影响（读的仍是那个默认值）。
    """
    monkeypatch.setattr(_harness(), "LOG_PATH", tmp_path / "probe.log")


def _cell(task: str, arm: str, passed: bool, *, temperature: float = 0.0, label: str = "") -> dict:
    """一格读数的最小形状：判据一条，通过与否由 `passed` 定。"""
    return {
        "label": label or f"{task}/t{temperature}/{arm}/r0",
        "task": task,
        "arm": arm,
        "temperature": temperature,
        "checks": {"判据": bool(passed), "_hits": {"判据": []}},
    }


def _full_row(task: str, arm: str, passed: bool, *, temperature: float = 0.0) -> dict:
    """`_summary` 要的全部字段都补齐的一格（数值给 0，只验结构）。"""
    row = _cell(task, arm, passed, temperature=temperature)
    row.update(
        {
            "finished": bool(passed),
            "steps": 1,
            "model_calls": 1,
            "prompt_tokens": 10,
            "completion_tokens": 10,
            "wall_seconds": 1.0,
            "model_seconds": 0.5,
            "tool_seconds": 0.5,
            "tool_calls": 1,
            "orchestrate_calls": 0,
            "subcalls": 0,
            "model_multi_call_turns": 0,
            "model_max_calls_in_turn": 1,
            "orchestrate_plans_ok": 0,
            "orchestrate_plans_rejected": 0,
        }
    )
    return row


# ===========================================================================
# 角色：每个靶都要有，且 A 是控制靶、B 是能力靶（这就是 S2 的裁决）
# ===========================================================================


def test_每个靶都有角色():
    module = _harness()
    for task in module.GOALS:
        assert task in module.TARGET_ROLES, f"靶 {task} 没有角色：控制靶还是能力靶，必须写死"
        assert module.TARGET_ROLES[task] in ("control", "ability")


def test_没有角色的靶走最保守的能力靶口径():
    """加了新靶却忘了写角色时，不能默默当控制靶（那会把它两臂都过头当成通过）。"""
    module = _harness()
    readings = [_cell("未登记的靶", "off", True), _cell("未登记的靶", "on", True)]
    verdict = module.target_verdict(readings, "未登记的靶")
    assert verdict["role"] == "ability"
    assert verdict["verdict"] == "no_discrimination"


def test_A靶是控制靶B靶是能力靶():
    module = _harness()
    assert module.TARGET_ROLES["A-todo"] == "control"
    assert module.TARGET_ROLES["B-failing-test"] == "ability"


# ===========================================================================
# 控制靶：各臂都应通过；哪格不过先查测量环境
# ===========================================================================


def test_控制靶全过给通过判词():
    module = _harness()
    readings = [_cell("A-todo", arm, True) for arm in ("off", "on", "control")]
    verdict = module.target_verdict(readings, "A-todo")
    assert verdict["verdict"] == "control_ok"
    assert "next" not in verdict
    assert verdict["cells"] == 3


def test_控制靶有格没过先说查环境():
    """A 靶当初就是这么坏的：子进程 PATH 里没有 rg → 两臂都 0/3，被记成模型不行。"""
    module = _harness()
    readings = [_cell("A-todo", "off", False), _cell("A-todo", "on", True)]
    verdict = module.target_verdict(readings, "A-todo")
    assert verdict["verdict"] == "control_failed"
    assert "环境" in verdict["next"]
    assert "别记成模型不行" in verdict["next"]
    assert verdict["pass_by_arm"] == {"off": {"pass": 0, "of": 1}, "on": {"pass": 1, "of": 1}}


# ===========================================================================
# 能力靶：必须量出差别
# ===========================================================================


def test_能力靶两臂有差别才算有区分度():
    module = _harness()
    readings = [
        _cell("B-failing-test", "off", False, label="B/t0/off/r0"),
        _cell("B-failing-test", "off", False, label="B/t0/off/r1"),
        _cell("B-failing-test", "on", True, label="B/t0/on/r0"),
        _cell("B-failing-test", "on", False, label="B/t0/on/r1"),
    ]
    verdict = module.target_verdict(readings, "B-failing-test")
    assert verdict["verdict"] == "discriminating"
    assert "next" not in verdict


def test_能力靶两臂全过是无区分度():
    module = _harness()
    readings = [_cell("B-failing-test", "off", True), _cell("B-failing-test", "on", True)]
    verdict = module.target_verdict(readings, "B-failing-test")
    assert verdict["verdict"] == "no_discrimination"
    assert "控制靶" in verdict["next"]


def test_能力靶两臂全不过是无区分度且先查难度与环境():
    module = _harness()
    readings = [_cell("B-failing-test", "off", False), _cell("B-failing-test", "on", False)]
    verdict = module.target_verdict(readings, "B-failing-test")
    assert verdict["verdict"] == "no_discrimination"
    assert "难度" in verdict["next"]
    assert "环境" in verdict["next"]


def test_没有读数就记没有读数():
    module = _harness()
    verdict = module.target_verdict([_cell("A-todo", "off", True)], "B-failing-test")
    assert verdict["verdict"] == "no_readings"
    assert verdict["cells"] == 0
    assert "pass_by_arm" not in verdict


def test_每个靶都出判词():
    module = _harness()
    verdicts = module.target_verdicts([_cell("A-todo", "off", True)])
    assert set(verdicts) == set(module.GOALS)
    assert verdicts["A-todo"]["verdict"] == "control_ok"
    assert verdicts["B-failing-test"]["verdict"] == "no_readings"


# ===========================================================================
# 判据本身：`_hits` 是明细不是判据
# ===========================================================================


def test_一格的_hits不参与判据():
    module = _harness()
    assert module._cell_passed({"checks": {"判据": True, "_hits": {"判据": []}}}) is True
    assert module._cell_passed({"checks": {"判据": True, "_hits": {"判据": [1, 2, 3]}}}) is True
    assert module._cell_passed({"checks": {"判据": False, "_hits": {"判据": [1]}}}) is False


def test_只有明细没有判据不算通过():
    module = _harness()
    assert module._cell_passed({"checks": {"_hits": {"判据": [1]}}}) is False
    assert module._cell_passed({}) is False


def test_任何下划线开头的明细都不算判据():
    """`_hits` / `_paths` 都是明细。只挑 `_hits` 一个名字排除，日后加明细就会漏。"""
    module = _harness()
    assert module._cell_passed({"checks": {"判据": True, "_paths": [1, 2]}}) is True
    assert module._cell_passed({"checks": {"_paths": [1]}}) is False


# ===========================================================================
# S7：B 靶的判据要挡得住「我查不出来」这种答复
# ===========================================================================

#: 逐字取自 p0 真机 `converge|r1` 的结论原文。旧判据（裸文件名 + 宽记号）给了它 2/2，
#: 而它答的其实是「我没查到」。
NEGATIVE_B_ANSWER = (
    "目前只能确认：测试目录为 tests/，其下有 context、fetch、health、services 四个子目录；"
    "尚未拿到失败测试名、报错信息、具体测试文件与相关实现，因此无法确定失败测试涉及哪些文件，"
    "也无法给出可执行修复计划。已查过：tests 目录列表；尝试读取 tests/test_service_ledger.py "
    "失败，说明该路径不存在。缺：失败测试名称、pytest 输出/断言错误、对应测试文件路径、相关实现文件路径与内容。"
)

#: 逐字取自 p0 真机 `base|r0` 的结论原文：这才是答对了的样子。
POSITIVE_B_ANSWER = (
    "失败测试：tests/services/test_service_ledger.py::test_停掉之后台账里要写明已经停过。"
    "涉及文件：src/spoolkit/service_ledger.py、tests/services/test_service_ledger.py。"
    "修复计划：在 src/spoolkit/service_ledger.py 的 stop() 中补 entry.stopped = True，然后运行该测试验证。"
)

#: 逐字取自真机 `on|r1` 的结论原文：它答的是「我做了什么」，没答「涉及哪些文件」。
WORK_REPORT_B_ANSWER = (
    "已修复 src/spoolkit/service_ledger.py 中 stop 函数缺少 entry.stopped = True 的 bug。"
    "测试全部通过（45 passed）。"
)


def test_答不出东西的结论不算通过():
    module = _harness()
    checks = module.checks_for("B-failing-test", NEGATIVE_B_ANSWER)
    assert checks[module.CHECKS["B"][0]] is False, "它连正确的测试文件路径都没写出来"
    assert checks[module.CHECKS["B"][1]] is False, "「无法给出可执行修复计划」不算给出计划"
    assert module._cell_passed({"checks": checks}) is False


def test_真答出来的结论才算通过():
    module = _harness()
    checks = module.checks_for("B-failing-test", POSITIVE_B_ANSWER)
    assert all(checks[one] for one in module.CHECKS["B"])
    assert module._cell_passed({"checks": checks}) is True


def test_工作汇报不算答对问的问题():
    module = _harness()
    checks = module.checks_for("B-failing-test", WORK_REPORT_B_ANSWER)
    assert checks[module.CHECKS["B"][0]] is False
    assert checks[module.CHECKS["B"][1]] is False


def test_判据只认带目录的路径与肯定式计划():
    module = _harness()
    assert module.CHECK_B_PLAN_RE.search("修复计划：补 entry.stopped = True")
    assert module.CHECK_B_PLAN_RE.search("建议 1) 在 stop() 里补上赋值")
    assert not module.CHECK_B_PLAN_RE.search("无法给出可执行修复计划。")
    checks = module.checks_for(
        "B-failing-test", "见 service_ledger.py 与 test_service_ledger.py"
    )
    assert checks[module.CHECKS["B"][0]] is False, "裸文件名不再算数，要把路径写出来"


def test_判词取值都在封闭集合里():
    module = _harness()
    cases = [
        ([], "no_readings"),
        ([_cell("A-todo", "off", True)], "control_ok"),
        ([_cell("A-todo", "off", False)], "control_failed"),
        ([_cell("B-failing-test", "off", True), _cell("B-failing-test", "on", False)], "discriminating"),
        ([_cell("B-failing-test", "off", True), _cell("B-failing-test", "on", True)], "no_discrimination"),
    ]
    for readings, expected in cases:
        task = readings[0]["task"] if readings else "A-todo"
        verdict = module.target_verdict(readings, task)
        assert verdict["verdict"] == expected
        assert verdict["verdict"] in module.TARGET_VERDICTS


# ===========================================================================
# 落盘与汇总：判词必须进读数文件与汇总表，不能只活在函数里
# ===========================================================================


def test_读数文件里带着靶角色与靶判词(tmp_path, monkeypatch):
    module = _harness()
    real_done = module.DONE_FILE
    done_before = real_done.read_text(encoding="utf-8") if real_done.is_file() else None
    monkeypatch.setattr(module, "OUT", tmp_path)
    monkeypatch.setattr(module, "STATE_FILE", tmp_path / "state.json")
    # `DONE_FILE` 是 import 时就定死的模块级常量，**必须连它一起换掉**：
    # `ORCH_SKIP_DONE` 只管「读旧标签合并」，**不阻止写入**——只换 `OUT` 的话
    # `_write_done` 会照旧往仓库里写 `real-done.json`（曾经真踩到过，账本里多出
    # 4 个格子标签，差点带进提交）。
    monkeypatch.setattr(module, "DONE_FILE", tmp_path / "real-done.json")
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    readings = [
        _full_row("A-todo", "off", True),
        _full_row("A-todo", "on", True),
        _full_row("B-failing-test", "off", False),
        _full_row("B-failing-test", "on", True),
    ]
    module._flush(readings, [], [0.0], 1, 60, {"head": "deadbeef"}, time.time())

    payload = json.loads((tmp_path / "real-readings.json").read_text(encoding="utf-8"))
    assert payload["target_roles"] == module.TARGET_ROLES
    assert payload["target_verdicts"]["A-todo"]["verdict"] == "control_ok"
    assert payload["target_verdicts"]["B-failing-test"]["verdict"] == "discriminating"
    # 用例外一条底线：仓库里那本账一个字节都不许动。
    done_after = real_done.read_text(encoding="utf-8") if real_done.is_file() else None
    assert done_after == done_before, "这条用例把仓库的 real-done.json 写了"


def test_汇总表里的格子带角色与通过数():
    module = _harness()
    readings = [
        _full_row("A-todo", "off", True),
        _full_row("A-todo", "off", False),
        _full_row("B-failing-test", "on", True),
    ]
    summary = module._summary(readings, [0.0])
    control = summary["A-todo/t0.0/off"]
    assert control["role"] == "control"
    assert control["passed"] == 1
    assert control["runs"] == 2
    ability = summary["B-failing-test/t0.0/on"]
    assert ability["role"] == "ability"
    assert ability["passed"] == 1


def test_真读数文件的结构不变量():
    """只看结构：S12 以后会重跑并覆盖具体判词，具体结论不能写死在用例里。"""
    module = _harness()
    path = HARNESS_DIR / "real-readings.json"
    if not path.is_file():  # pragma: no cover - 读数文件是提交物，正常在
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    roles = payload.get("target_roles") or {}
    assert roles == module.TARGET_ROLES
    for task, verdict in (payload.get("target_verdicts") or {}).items():
        assert task in module.GOALS
        assert verdict["role"] == roles[task]
        assert verdict["verdict"] in module.TARGET_VERDICTS


# ===========================================================================
# S3-a：副本现场必须**只有那一条红**（否则读的是副本，不是模型）
# ===========================================================================


def test_副本不再替换真源码():
    """S3-a 的裁决：fixture 只加不减。旧版替换 `src/spoolkit/services.py` 时，
    副本里 26 条收集错误把目标测试埋掉，B 靶读到的是噪声。"""
    module = _harness()
    recipe = module.sandbox_recipe()
    targets = [relative for relative, _ in recipe["write"]]
    assert targets == [
        "src/spoolkit/service_ledger.py",
        "tests/services/test_service_ledger.py",
    ]
    assert not any("services.py" in one and "ledger" not in one for one in targets)
    assert module.CHECK_B_PATHS == (
        "tests/services/test_service_ledger.py",
        "src/spoolkit/service_ledger.py",
    )


def test_副本抄了探针脚本():
    """`tests/test_probe_*.py` / `test_envcheck.py` 会 `import probe`，副本要自带它。"""
    module = _harness()
    targets = [target for _, target in module.sandbox_recipe()["copy"]]
    assert "scripts/probe.py" in targets


def test_裁剪白名单只留干净的目录() -> None:
    """白名单是量出来的：`context`/`health`/`fetch` 三个目录在副本里 0 红，
    其余目录成片 `PermissionError`（缺 `tests/conftest.py` 铺临时根）。"""
    module = _harness()
    keep = tuple(module.SANDBOX_TEST_KEEP)
    assert keep == (
        "tests/services/test_service_ledger.py",
        "tests/context",
        "tests/health",
        "tests/fetch",
    )


def test_裁剪清单里的东西都得存在() -> None:
    module = _harness()
    writes = {relative for relative, _ in module.sandbox_recipe()["write"]}
    for item in module.SANDBOX_TEST_KEEP:
        assert (REPO / item).exists() or item in writes, item


def test_裁剪函数只留白名单(tmp_path: Path) -> None:
    module = _harness()
    for relative in (
        "tests/services/test_service_ledger.py",
        "tests/services/test_load.py",
        "tests/context/test_budget.py",
        "tests/web/sub/test_page.py",
        "tests/cli/test_x.py",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    summary = module._prune_tests(
        tmp_path,
        keep=("tests/services/test_service_ledger.py", "tests/context"),
    )

    assert (tmp_path / "tests/services/test_service_ledger.py").is_file()
    assert (tmp_path / "tests/context/test_budget.py").is_file()
    assert not (tmp_path / "tests/services/test_load.py").exists()
    assert not (tmp_path / "tests/web/sub/test_page.py").exists()
    assert not (tmp_path / "tests/cli/test_x.py").exists()
    # 空目录要顺手清掉：留着会让 `list_dir` 看见一堆没内容的目录
    assert not (tmp_path / "tests/cli").exists()
    assert summary["kept"] == 2
    assert summary["removed"] == 3


def test_裁剪清单写了不存在的东西就报错(tmp_path: Path) -> None:
    module = _harness()
    (tmp_path / "tests").mkdir()
    with pytest.raises(RuntimeError):
        module._prune_tests(tmp_path, keep=("tests/nope/test_x.py",))


def test_建造副本时会裁剪而且阶段零会量副本干不干净() -> None:
    """两件事必须在代码里：`_build_sandbox` 调裁剪；`stage_sandbox` 量裸跑并
    在「不是恰好 1 条 FAILED、0 条 ERROR」时**响亮失败**（抛异常，不开跑）。"""
    module = _harness()
    source = HARNESS.read_text(encoding="utf-8")
    assert "pruned = _prune_tests(project)" in source
    assert "bare_failed" in source and "bare_errors" in source
    assert "len(bare_failed) != 1 or bare_errors" in source
    assert "raise RuntimeError(hint)" in source
    # 量出来的东西要落盘，别只打印
    assert '"bare_failed": bare_failed,' in source
    assert module.SANDBOX_TEST_KEEP


def test_批次窗口取自环境变量(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`ORCH_WINDOW` 是这一批的自变量，必须真的改变循环窗口。

    S6 踩到的正是这里：环境变量只喂给环境自证，循环却硬编码 8192，
    于是「换个窗口看预算阶梯」这件事一次都没量到。
    """
    module = _harness()
    monkeypatch.setenv("ORCH_WINDOW", "4096")
    assert module.batch_config(tmp_path).context_window == 4096
    monkeypatch.delenv("ORCH_WINDOW")
    assert module.batch_config(tmp_path).context_window == 8192


# --- S7：每一次真机跑之前都要重摆沙箱 -----------------------------------------
#
# 沙箱原来是**整批共用一份**，于是第一跑把失败测试的 bug 修好之后，后面的每一跑
# 都开始于全绿的现场——而这个靶问的正是「哪条测试还红着」。臂的顺序是
# off → on → control，排在后面的臂系统性地吃亏，那不是模型能力差异。证据在
# `benchmarks/probes/orchestration/README-real.md` 的 S7 一节。


def test_重摆沙箱先删上一份再调造副本(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`reset_sandbox` 的契约：**先删上一份、再造一份新的，返回新的路径**。"""
    module = _harness()
    old = tmp_path / "spool-orch-real-old" / "project"
    old.mkdir(parents=True)
    state_file = tmp_path / ".real-state.json"
    state_file.write_text(json.dumps({"project": str(old)}), encoding="utf-8")
    removed: list[object] = []
    monkeypatch.setattr(module, "remove_sandbox", lambda project: removed.append(project) or True)
    staged: list[int] = []

    def stage() -> None:
        staged.append(1)
        fresh = tmp_path / "spool-orch-real-new" / "project"
        fresh.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({"project": str(fresh)}), encoding="utf-8")

    project = module.reset_sandbox(state_file=state_file, stage=stage)

    assert removed == [str(old)], "要删的是上一次那份沙箱"
    assert staged == [1], "重摆必须真的造一份新的"
    assert project == str(tmp_path / "spool-orch-real-new" / "project")


def test_删沙箱认不出路径就不删(tmp_path: Path) -> None:
    """删沙箱只能删**我们自己造的**那种路径：否则一次误传就把别人的工作区删了。"""
    module = _harness()
    outsider = tmp_path / "someones-workspace" / "project"
    outsider.mkdir(parents=True)
    canary = outsider / "keep.txt"
    canary.write_text("别删我\n", encoding="utf-8")
    assert module.remove_sandbox(str(outsider)) is False
    assert module.remove_sandbox(None) is False
    assert canary.is_file(), "认不出的路径一个字节都不许动"
    # 我们自己造的那种路径才认
    assert "spool-orch-real-" in module.SANDBOX_PREFIXES


def test_两个探针每跑之前都重摆沙箱() -> None:
    """接线也要钉住：两个探针的 `run_once` 里都得有重摆这一步。"""
    p0 = HARNESS_DIR / "p0_converge_probe.py"
    p0_source = p0.read_text(encoding="utf-8")

    def run_once_body(path: Path) -> str:
        text = path.read_text(encoding="utf-8")
        assert "def run_once(" in text, f"{path.name} 里没有 run_once"
        return text.split("def run_once(", 1)[1].split("\ndef ", 1)[0]

    assert "reset_sandbox()" in run_once_body(HARNESS)
    assert "reset_sandbox(" in run_once_body(p0)
    # p0 用的是同一份实现（不抄第二份删/造逻辑）
    assert "reset_sandbox," in p0_source
    assert "from real_counterfactual import" in p0_source


def test_p0有两条写额度对照臂且读数带计数() -> None:
    """**O6/O8 的对照必须能被读出来**：臂在 `ARMS` 里，计数在 `run_once` 与汇总里。

    三件事，缺一条这条实验就量不出来：

    1. 两个对照臂只差 `converge_write_grace`（0 / 1），其余 overrides 与
       `converge` 臂逐字相同——否则差别不只一个变量；
    2. 记录里有 `write_allowed` / `write_blocked`：没有它们，「收束期到底拦了几次
       写」只能靠人肉翻事件流；
    3. 汇总也带上这两列，报告才能按臂比。
    """
    p0 = HARNESS_DIR / "p0_converge_probe.py"
    source = p0.read_text(encoding="utf-8")
    arms_text = source.split("ARMS: dict", 1)[1].split("\n}\n", 1)[0]
    # ① 臂的形状：与 converge 臂同源，只多一个键。
    assert '"converge-nowrite"' in arms_text and '"converge-onewrite"' in arms_text
    for arm in ("converge-nowrite", "converge-onewrite"):
        block = arms_text.split(f'"{arm}"', 1)[1].split("}", 1)[0]
        assert '"step_budget_hard_cap": 0' in block, block
        assert '"converge_at_steps": 3' in block, block
    assert '"converge_write_grace": 0' in arms_text
    assert '"converge_write_grace": 1' in arms_text
    # `converge` 对照臂必须**显式**钉住 -1：这一档的默认值已经是 0，省略就两臂同配、
    # 对照实验静默失效。
    converge_block = arms_text.split('"converge":', 1)[1].split("}", 1)[0]
    assert '"converge_write_grace": -1' in converge_block, converge_block
    # ② 记录与 ③ 汇总都带这两列。
    assert source.count('"write_allowed"') >= 2, "记录与汇总都要有 write_allowed"
    assert source.count('"write_blocked"') >= 2, "记录与汇总都要有 write_blocked"
    # 两条计数从收束事件里数出来（`kind="converge"` + `action`），不是猜的。
    assert 'one.get("action") == "write_allowed"' in source
    assert 'one.get("action") == "write_blocked"' in source
    # 汇总里的判据明细按 `_` 前缀排除（`_paths` 也是明细，不只是 `_hits`）。
    summary = source.split("def _summary(", 1)[1].split("\ndef ", 1)[0]
    assert 'if not key.startswith("_")' in summary, summary
    assert 'key != "_hits"' not in summary, "明细的排除口径退回旧写法了"


def test_真机探针读数也带收束写计数() -> None:
    """默认改成「收束期拦写」之后，真机读数必须看得见它到底拦到没有。

    B 靶那三臂用的是**默认配置**（`off` / `on` / `control` 只差编排这一件事），
    所以 `converge_write_grace=0` 在真机批次里是真的生效的。读数里没有这两列的话，
    「默认开了、一次没拦」和「默认没开」在报告里长得一模一样。
    """
    source = HARNESS.read_text(encoding="utf-8")
    run_once = source.split("def run_once(", 1)[1].split("\ndef ", 1)[0]
    assert 'data.get("kind") == "converge"' in run_once
    assert 'data.get("action") == "write_allowed"' in run_once
    assert 'data.get("action") == "write_blocked"' in run_once
    assert '"write_allowed": write_allowed' in run_once
    assert '"write_blocked": write_blocked' in run_once


def test_真机探针读数带重复执行与拦下计数() -> None:
    """S9 关掉的是「判不了就放行」，真机读数必须看得见「同一件事重跑了没有」。

    护栏的拦截在事件流里并不都有留痕（「无用重复」那一档只回一条 `ok=False` 的
    结果），所以读数靠绕一层**只读**处理器：包 `registry.invoke` 数真执行过的调用，
    包 `_refuse` 收拦下来的话术。没有这两份账，「拦住了」与「压根没重复」在报告里
    长得一模一样。
    """
    source = HARNESS.read_text(encoding="utf-8")
    run_once = source.split("def run_once(", 1)[1].split("\ndef ", 1)[0]
    instrument = source.split("def instrument_guards(", 1)[1].split("\ndef ", 1)[0]
    readings = source.split("def guard_readings(", 1)[1].split("\ndef ", 1)[0]
    # ① 包的是真实现，不是自己重写一份。
    assert "real_invoke = loop.registry.invoke" in instrument
    assert "real_refuse = loop._refuse" in instrument
    assert "loop.registry.invoke = counting_invoke" in instrument
    assert "loop._refuse = counting_refuse" in instrument
    # ② 「同一次调用」按「工具 + 参数逐字」归一，不看名字。
    assert "json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)" in instrument
    # ③ 两档拒绝按话术分开数（判不出 / 无用重复），不是合成一个数。
    assert "与上一次**完全相同**" in readings
    assert "UNDECIDED_MARKS" in source
    assert "没有可比较的内容对象" in source
    assert "没有真的执行过的记录" in source
    # 拦截要记**工具名**：报告要说清「拦的是哪一件」，只给总数等于没说。
    assert 'book["refusals"].append((call.name, str(message)))' in instrument
    assert '"refused_useless_tools": useless' in readings
    assert '"repeat_tools": repeated' in readings
    # ④ 记录里读的是这四个字段，且探针真的调了这两个助手。
    assert "book = instrument_guards(loop)" in run_once
    assert "guards = guard_readings(book)" in run_once
    for field in ("repeat_executions", "refused_useless", "refused_undecided", "command_runs"):
        assert f'"{field}": {field}' in run_once, field
    # ⑤ P0 探针（紧步数那一档）走**同一份**实现，不抄第二份读数口径。
    other = (HARNESS_DIR / "p0_converge_probe.py").read_text(encoding="utf-8")
    assert "guard_readings," in other and "instrument_guards," in other, "P0 没接同一份读数"
    assert "guard_book = instrument_guards(loop)" in other
    assert "guard = guard_readings(guard_book)" in other
    for field in ("repeat_executions", "refused_useless", "refused_undecided", "command_runs"):
        assert f'"{field}": guard["{field}"]' in other, field
