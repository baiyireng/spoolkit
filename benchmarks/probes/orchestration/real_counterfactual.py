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
任务 B：① 点名了涉及的两条**文件路径**（带目录，fixture 自己声明的）；② 给出肯定式
修复计划（记号后面紧跟冒号或数字）。机械判据，只认关键词，不看文采。
这样做是为了挡掉**否定式答复**：S7 实测里有一格的结论写着「尝试读取
`tests/test_service_ledger.py` 失败…无法给出可执行修复计划」，裸文件名 + 宽记号会
把它判成 2/2（详见 `CHECK_B_PATHS` / `CHECK_B_PLAN_RE` 的注释）。

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
import re
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
from spoolkit import envcheck  # noqa: E402  (src 已在上面挂进 sys.path)

#: task B 的失败测试 fixture。为什么自己造一个而不是沿用上一轮的
#: `tests/cli/test_service_command.py`：那个文件在**本机沙箱里是红的**
#: （taskkill 被拦），红的原因不是代码而是环境，模型给出的「修复计划」会指向
#: 沙箱而不是缺陷。这里造一个**确定性失败**：一行代码少了 `stopped = True`，
#: pytest 断言红，失败信息指向缺陷那一行。
#:
#: **为什么不再替换 `src/spoolkit/services.py`（2026-10-07 实测纠正）**：
#: 上一版用一个精简 `services.py` 顶掉真文件，它少了 `render_status`——而
#: `spoolkit.cli.app` 一路 import 到它，于是副本里 **23 个测试文件收集即
#: ImportError**（17 个 `tests/cli/*` + `tests/kernel/test_merge_blockers.py` +
#: `tests/memory/test_dryrun_readonly.py` + `tests/memory/test_migrate_command.py` +
#: `tests/test_channel_options.py` + `tests/test_end_to_end.py`），再加 3 个
#: `import probe` 失败，裸 `python -m pytest -q` 直接
#: `Interrupted: 26 errors during collection`——**目标测试根本没跑起来**。
#: 模型拿到的失败要点就是那 26 条 `ERROR tests/cli/...`，于是照着第一条回答
#: `tests/cli/test_approve_command.py`：那不是幻觉，是照着被截断的现场读出来的。
#: 这一版**只加不减**：真 `services.py` 原样保留，另加一个自洽模块与它的测试，
#: 副本能正常收集、目标那一条 FAILED 才可能被看见。
FIXTURE_LEDGER = '''"""本机服务的台账：拉起 / 停掉各记一笔，按记录判状态。

台账是给 `spool status` 看的，所以「停掉」必须在记录里留下痕迹：进程自己退出
之后，内存里的那个布尔值没人看得见，下一次 start 会照着一条过期记录说
「已经在跑」，用户以为自己有服务、其实一个进程都没有。
"""

from __future__ import annotations

import json
from pathlib import Path

STATE = Path(__file__).resolve().parent / "ledger.json"


class Entry:
    """一条服务记录。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.running = False
        self.pid: int | None = None
        self.stopped = False


def _read_record() -> dict:
    if not STATE.is_file():
        return {}
    return json.loads(STATE.read_text(encoding="utf-8"))


def _write_record(payload: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(payload), encoding="utf-8")


def start(entry: Entry) -> int:
    """拉起：已经有活着的记录就不重复拉起（判据是记录，不是探针）。"""
    record = _read_record()
    if record.get("name") == entry.name and record.get("alive"):
        return int(record["pid"])
    _write_record({"name": entry.name, "pid": 4242, "alive": True})
    entry.pid = 4242
    entry.running = True
    return 4242


def stop(entry: Entry) -> None:
    """停掉：写一笔「已停」，并清掉活着的标记。"""
    _write_record({"name": entry.name, "pid": entry.pid, "alive": False})
    entry.running = False
    # BUG：这里少了 entry.stopped = True
'''

FIXTURE_LEDGER_TEST = '''"""`service_ledger` 的行为：判据是台账记录，不是内存里的那个布尔值。"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import spoolkit.service_ledger as ledger  # noqa: E402
from spoolkit.service_ledger import Entry, start, stop  # noqa: E402


def _fresh_workdir(name: str) -> Path:
    """本用例自己的现场目录（不用 pytest 的 tmp_path，免得跟平台临时根纠缠）。"""
    base = Path(__file__).resolve().parent / f"_work_{name}"
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    return base


def test_拉起之后台账里是活着的(monkeypatch) -> None:
    workdir = _fresh_workdir("start")
    monkeypatch.setattr(ledger, "STATE", workdir / "ledger.json")

    assert start(Entry("tunnel")) == 4242

    assert ledger._read_record()["alive"] is True


def test_停掉之后台账里要写明已经停过(monkeypatch) -> None:
    workdir = _fresh_workdir("stop")
    monkeypatch.setattr(ledger, "STATE", workdir / "ledger.json")
    entry = Entry("tunnel")
    start(entry)

    stop(entry)

    assert entry.stopped is True, "停掉之后 entry.stopped 必须为真"
    record = ledger._read_record()
    assert record["alive"] is False, f"台账里的 alive 要为假：{record}"
'''


#: 副本里留下的测试（白名单）。实测（2026-10-07，`.tmp/s3_curate.py`）：整棵
#: `tests/` 拷进副本、又不抄 `tests/conftest.py` 时，凡是用 `tmp_path` 或平台临时根
#: 的用例会成片 `PermissionError: [WinError 5]`（memory 381 条、tools 316 条、
#: cli 272 条、web 194 条……），唯一那条真 FAILED 会被两千多条环境噪声埋掉——
#: 那样测的是副本，不是模型。所以副本只留**在这个环境里干净**的目录，外加要模型
#: 找的那条失败测试和它旁边的正常测试。（`tests/services/test_load.py` 也在这一类
#: 噪声里：它写平台临时根，副本里 3 条 ERROR，所以一并裁掉。）
#: 不变量由 `stage_sandbox()` 把关：副本里裸 `pytest -q` 必须恰好 1 条 FAILED、0 条 ERROR。
SANDBOX_TEST_KEEP = (
    "tests/services/test_service_ledger.py",
    "tests/context",
    "tests/health",
    "tests/fetch",
)


def _prune_tests(project: Path, keep=SANDBOX_TEST_KEEP) -> dict:
    """把副本的 `tests/` 裁到白名单，返回留下/删掉的数量。

    白名单里给目录表示「这一棵都要」；给文件表示「只要这个文件」。
    清单里写了一个不存在的东西就直接报错——静默少留东西会让不变量悄悄失效。
    """
    tests_root = project / "tests"
    keep_list = [(project / item).resolve() for item in keep]

    def wanted(target: Path) -> bool:
        resolved = target.resolve()
        return any(resolved == one or one in resolved.parents for one in keep_list)

    kept = 0
    removed = 0
    for path in sorted(tests_root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_dir():
            if not any(path.iterdir()):
                path.rmdir()
            continue
        if wanted(path):
            kept += 1
        else:
            path.unlink()
            removed += 1
    missing = [item for item in keep if not (project / item).exists()]
    if missing:
        raise RuntimeError(f"副本裁剪清单里有不存在的东西：{missing}")
    return {"kept": kept, "removed": removed, "whitelist": list(keep)}


def sandbox_recipe() -> dict:
    """沙箱配方：复制什么、预置什么。两个臂逐字相同。"""
    return {
        "copy": (
            ("src/spoolkit", "src/spoolkit"),
            ("tests", "tests"),
            ("pyproject.toml", "pyproject.toml"),
            # 副本里 `tests/test_probe_runner.py` / `test_probe_stability.py` /
            # `test_envcheck.py` 会 `import probe`：真仓库靠测试自己把 `scripts`
            # 插进 `sys.path`，而这个副本必须自带那一份，否则 pytest 收集阶段
            # 就是 3 条 `ModuleNotFoundError: No module named 'probe'`。
            ("scripts/probe.py", "scripts/probe.py"),
        ),
        "seed_todos": SEED_FILES,
        "seed_mark": SEED_MARK,
        "write": (
            ("src/spoolkit/service_ledger.py", FIXTURE_LEDGER),
            ("tests/services/test_service_ledger.py", FIXTURE_LEDGER_TEST),
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
    # 裁剪必须在写 fixture 之后：白名单里就有刚写进去的那条失败测试。
    pruned = _prune_tests(project)
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
    return (
        f"预置 {seeded}/{len(recipe['seed_todos'])} 条待办，"
        f"副本测试裁到 {pruned['kept']} 个文件（删掉 {pruned['removed']} 个）"
    )


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
    # 与探针同一条补 PATH 的路（候选目录只有一份实现，见 spoolkit.envcheck）。
    path_added = envcheck.ensure_probe_tools(env, ("rg", "node"))

    def run_pytest(target: str | None = None) -> tuple[subprocess.CompletedProcess, float]:
        """在副本里跑一次 pytest。`target` 为 None 就是裸 `pytest -q`（模型会这么干）。"""
        argv = [
            str(python),
            "-m",
            "pytest",
            "-q",
            # 沙箱里建不出 `.pytest_cache`（受限目录），关掉缓存插件免得噪声
            # 盖住真正的失败原文。用例本身不用 tmp_path，所以也不碰平台临时根。
            "-p",
            "no:cacheprovider",
        ]
        if target:
            argv.append(target)
        began = time.perf_counter()
        done = subprocess.run(
            argv,
            cwd=str(project),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        return done, time.perf_counter() - began

    # 第一件事：量副本自己干不干净。模型大概率一上来就敲 `pytest -q`，而这批读数
    # 要回答的是「它能不能找到那条失败测试」，所以现场必须**只有那一条红**。
    whole, whole_seconds = run_pytest()
    whole_raw = (whole.stdout or "") + (whole.stderr or "")
    (OUT / "real-sandbox-whole.txt").write_text(whole_raw, encoding="utf-8")
    bare_failed = [
        line.strip() for line in whole_raw.splitlines() if line.strip().startswith("FAILED ")
    ]
    bare_errors = [
        line.strip() for line in whole_raw.splitlines() if line.strip().startswith("ERROR ")
    ]
    if len(bare_failed) != 1 or bare_errors:
        hint = (
            "副本自己就不干净：裸 `pytest -q` 应当恰好 1 条 FAILED、0 条 ERROR。"
            "先修 `sandbox_recipe()` 与 `SANDBOX_TEST_KEEP`，别拿被污染的现场去读模型能力。"
        )
        say(f"⚠ 沙箱不变量被打破：FAILED {len(bare_failed)} 条、ERROR {len(bare_errors)} 条")
        say(hint)
        for line in (bare_failed[:5] + bare_errors[:5]):
            say(f"    {line}")
        raise RuntimeError(hint)

    finished, seconds = run_pytest("tests/services/test_service_ledger.py")
    # 失败原文按 utf-8 落一份：终端是 cp936，中文与替换字符会把 print 弄炸，
    # 而「这次真的红了」的证据必须在。日志走 `errors="replace"`。
    raw = (finished.stdout or "") + (finished.stderr or "")
    (OUT / "real-sandbox-pytest.txt").write_text(raw, encoding="utf-8")
    tail = "\n".join((finished.stdout or "").strip().splitlines()[-12:])
    say(f"沙箱 : {project}")
    say(f"配方 : {note}")
    say(
        f"副本裸跑：exit={whole.returncode}（{whole_seconds:.1f}s）"
        f"FAILED {len(bare_failed)} 条、ERROR {len(bare_errors)} 条"
    )
    say(f"失败测试：exit={finished.returncode}（{seconds:.1f}s）")
    say(tail)
    STATE_FILE.write_text(
        json.dumps(
            {
                "project": str(project),
                "todo_seed": len(SEED_FILES),
                "bare_exit": whole.returncode,
                "bare_failed": bare_failed,
                "bare_errors": bare_errors,
                "bare_seconds": round(whole_seconds, 2),
                "fail_exit": finished.returncode,
                "fail_tail": tail,
                "fail_seconds": round(seconds, 2),
                "path_added": path_added,
                "cleanup": "跑完用 `python <本脚本> clean` 删掉这个沙箱",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return 0


#: 我们造的沙箱目录前缀：本探针一个，p0 收束探针一个。删沙箱前必须对得上其中一个。
SANDBOX_PREFIXES = ("spool-orch-real-", "spool-p0-converge-")


def remove_sandbox(project: str | Path | None) -> bool:
    """删掉一份沙箱（它自己带 `project` 这一层，只删这一层）。路径形状不对就拒绝删。"""
    if not project:
        return False
    base = Path(project).resolve().parent
    if base.name == "project" or not any(one in base.name for one in SANDBOX_PREFIXES):
        say(f"拒绝删除（路径不像我们造的沙箱）：{base}")
        return False
    shutil.rmtree(base, ignore_errors=True)
    return True


def reset_sandbox(*, state_file: Path = STATE_FILE, stage=None) -> str:
    """重摆沙箱：删掉 `state_file` 里那一份，再调 `stage()` 造一份新的，返回新副本路径。

    **每一次真机跑之前都要调**。为什么（S7 实测）：沙箱是整批共用一份，而 B 靶的
    判据是「找出这条失败测试涉及哪些文件」——第一跑把 `stop()` 修好之后，后面的
    每一跑都开始于**已经全绿**的现场，判据从根上问不出来。臂的顺序是
    off → on → control，于是排在后面的臂系统性地吃亏，那不是模型能力差异。
    （证据：S6 那批 8192 读数里 `off|r1`/`on|r0`/`on|r1` 的 final 写着「45 passed」，
    而它们要回答的正是「哪条测试还红着」。）

    `stage` 不传就用本探针的 `stage_sandbox()`；p0 收束探针传它自己那个。
    """
    if state_file.is_file():
        state = json.loads(state_file.read_text(encoding="utf-8"))
        remove_sandbox(state.get("project"))
    (stage or stage_sandbox)()
    return str(json.loads(state_file.read_text(encoding="utf-8"))["project"])


def stage_clean() -> int:
    """删掉阶段 0 造的那个沙箱。"""
    if not STATE_FILE.is_file():
        print("没有 .real-state.json，没什么可删的")
        return 0
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    base = Path(state["project"]).resolve().parent
    if not remove_sandbox(state.get("project")):
        print(f"拒绝删除（路径不像我们造的沙箱）：{base}")
        return 2
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
#: 任务 B 的判据：涉及的文件**路径**（带目录，fixture 自己声明的）+ 修复计划的记号。
#:
#: S7 收紧了这两条，因为原来的裸文件名 + 宽记号会被**否定式答复**骗过：p0 真机
#: `converge|r1` 的结论原文是「…尝试读取 tests/test_service_ledger.py 失败，说明该
#: 路径不存在…无法确定失败测试涉及哪些文件，也无法给出可执行修复计划…」，判据却给了
#: 2/2——它答的是「我查不出来」。现在：
#:   - 认**带目录的完整路径**：否定式答复写不出正确路径（它猜的那个少了 `services/`）；
#:   - 计划记号必须**紧接着**冒号或数字（`修复计划：` / `建议 1)`），只出现「修复计划」
#:     四个字不算——「无法给出可执行修复计划」正是这样被挡下的。
CHECK_B_PATHS = ("tests/services/test_service_ledger.py", "src/spoolkit/service_ledger.py")
CHECK_B_PLAN = ("修复计划", "修复步骤", "修复方案", "修复建议", "改进计划", "建议", "步骤")
#: 肯定式计划：记号后面紧跟冒号或数字。
CHECK_B_PLAN_RE = re.compile(r"(修复计划|修复步骤|修复方案|修复建议|改进计划|建议|步骤)\s*(?:[:：]|\d)")
#: B 靶那条失败用例的名字（fixture 自己写的）。它**不是判据**，是一条机械读数：
#: 模型最后有没有真的看到那条失败原文。素材压缩（S6）的验收正是这一条——压完
#: 判据原文还在上下文里，模型才可能把它名字点出来；压缩把这些丢掉就会立刻显形。
CHECK_B_FAILING_TEST = "test_停掉之后台账里要写明已经停过"

CHECKS = {
    "A": ("覆盖预设待办（6/6）", "有分类", "有优先级"),
    "B": ("点名涉及的文件", "给出修复计划"),
}

#: 靶的角色。**这是 S2（O2）的裁决**，不是随手贴的标签。
#:
#: `A-todo` 原先当能力靶用，但它**没有区分度**：补上 rg 之后，off/on/control 三臂
#: 都是 3/3、3 步、30–36 秒（同一批格子此前报「0/3」是环境故障——子进程 PATH 里
#: 没有 rg，见 `spoolkit.envcheck`——不是模型不行）。一个三臂都做对的任务量不出
#: 「编排有没有用」。它真正的用处是**控制靶**：各臂都应该通过，哪一格没过就先查
#: 测量环境，而不是记成模型不行——这一条正是 A 靶当初抓出缺 rg 的原因。
#:
#: 能力靶的准入判据（同时写进 README-real.md）：**同一配方下必须至少有一格两臂
#: 判据不同**。两臂全过 = 无区分度，只能当控制靶；两臂全不过 = 也量不出差别，
#: 先查难度与环境。加新靶时按这条判，不为「看起来更难」而造。
TARGET_ROLES: dict[str, str] = {
    "A-todo": "control",
    "B-failing-test": "ability",
}

#: 判词取值（给读数文件与用例一个封闭集合，别让拼写漂移）。
TARGET_VERDICTS = (
    "control_ok",
    "control_failed",
    "discriminating",
    "no_discrimination",
    "no_readings",
)


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
        CHECKS["B"][0]: all(one in text for one in CHECK_B_PATHS),
        CHECKS["B"][1]: bool(CHECK_B_PLAN_RE.search(text)),
        "_paths": [one for one in CHECK_B_PATHS if one in text],
    }


def _cell_passed(row: dict) -> bool:
    """一格通过 = 它那条靶的**全部机械判据**通过（`_` 开头的是明细，不是判据）。"""
    checks = {
        key: value
        for key, value in (row.get("checks") or {}).items()
        if not str(key).startswith("_")
    }
    return bool(checks) and all(checks.values())


def target_verdict(readings: list[dict], task: str) -> dict:
    """按角色给一个靶下判词。纯函数：喂合成读数就能验，不需要真跑模型。

    - 控制靶：每格都必须通过；有格没过 → `control_failed`，**先查环境**。
    - 能力靶：至少一格两臂不同才算 `discriminating`；全过 → 无区分度（只能当控制
      靶）；全不过 → 也量不出差别（先查难度与环境，别急着改实现）。
    """
    rows = [one for one in readings if one.get("task") == task]
    role = TARGET_ROLES.get(task, "ability")
    verdict: dict = {"task": task, "role": role, "cells": len(rows)}
    if not rows:
        verdict["verdict"] = "no_readings"
        return verdict
    by_arm: dict[str, list[bool]] = {}
    for one in rows:
        by_arm.setdefault(str(one.get("arm", "?")), []).append(_cell_passed(one))
    verdict["pass_by_arm"] = {
        arm: {"pass": sum(1 for ok in flags if ok), "of": len(flags)}
        for arm, flags in sorted(by_arm.items())
    }
    all_pass = all(all(flags) for flags in by_arm.values())
    any_pass = any(any(flags) for flags in by_arm.values())
    if role == "control":
        verdict["verdict"] = "control_ok" if all_pass else "control_failed"
        if verdict["verdict"] == "control_failed":
            verdict["next"] = "先查测量环境（缺 rg / 代理劫持），别记成模型不行"
        return verdict
    if all_pass:
        verdict["verdict"] = "no_discrimination"
        verdict["next"] = "两臂全过，量不出差别：这个靶只能当控制靶（TARGET_ROLES）"
    elif not any_pass:
        verdict["verdict"] = "no_discrimination"
        verdict["next"] = "两臂全不过，量不出差别：先查难度与环境，别急着改实现"
    else:
        verdict["verdict"] = "discriminating"
    return verdict


def target_verdicts(readings: list[dict]) -> dict[str, dict]:
    """每个靶一条判词。`GOALS` 里有几个靶就出几条，缺读数的记 `no_readings`。"""
    return {task: target_verdict(readings, task) for task in GOALS}


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


def batch_config(project: Path):
    """这一批的循环配置。

    窗口是这一批的**自变量**：`ORCH_WINDOW` 以前只喂给环境自证，循环却一直按
    8192 跑——于是「换个窗口看预算阶梯」这件事根本量不到（S6 踩到）。
    """
    from spoolkit.config import Config

    return Config(
        project_root=project,
        supervise=False,
        max_steps=int(os.environ.get("ORCH_MAX_STEPS", "12")),
        context_window=int(os.environ.get("ORCH_WINDOW", "8192")),
    )


def run_once(task: str, arm: str, temperature: float, label: str) -> dict:
    from spoolkit.cli.runtime import LoopWiring, assemble_loop, counter_for
    from spoolkit.llm.llamacpp import LlamaCppGateway
    from spoolkit.tools.edit import PendingChanges

    # 每一次都记下**这一次跑的是哪个版本**。另一个写者会动工具索引/提示词，
    # 读数必须能按版本解释，不能笼统说「HEAD 是 X」。
    head = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=str(REPO), capture_output=True, text=True
    ).stdout.strip()
    # 每一次都**重摆沙箱**（见 `reset_sandbox()`）：不这么做的话，这一跑开始于
    # 上一跑改过的现场，B 靶的判据从第二跑起就问不出来了。
    project = Path(reset_sandbox())
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
        config=batch_config(project),
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
    judgement_lines_kept = 0
    write_allowed = 0
    write_blocked = 0
    for kind, data in events:
        if kind == "tool":
            tools.append(data)
            if data.get("name") == "orchestrate":
                orch_calls.append(data)
        elif kind == "subcall":
            subcalls.append(data)
        elif kind == "note" and data.get("kind") == "material_compacted":
            # 素材压缩（S6）保留了 N 行判据原文——这些行是逐字直通的，不是模型
            # 复述的。压缩只会在这一条事件上计数（`LoopResult.compactions` 同源）。
            judgement_lines_kept += int(data.get("protected_lines", 0) or 0)
        elif kind == "note" and data.get("kind") == "converge":
            # 收束期「写成果」的额度（S8，默认 0 = 收束期不再受理新改动）。真机读数
            # 里必须看得见它有没有真的生效：两列都为 0 也不奇怪（这一档只在收束
            # 期被踩到），但「默认开了却一次都没拦」与「默认没开」是两回事。
            if data.get("action") == "write_allowed":
                write_allowed += 1
            elif data.get("action") == "write_blocked":
                write_blocked += 1

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
        # S6：走的是哪一级（压了还是重置了）必须一起读。`compactions` 为 0 而
        # `resets` 照旧，说明这一级压根没参与；两个都高说明压完还得重置。
        "compactions": result.compactions,
        "judgement_lines_kept": judgement_lines_kept,
        # S8：收束期拦了几次写、放行了几次。默认 `converge_write_grace=0`。
        "write_allowed": write_allowed,
        "write_blocked": write_blocked,
        "final_names_failing_test": CHECK_B_FAILING_TEST in (result.final or ""),
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


def _isolate_local_http() -> dict[str, str]:
    """本机 8080 不走代理（唯一实现见 `spoolkit.envcheck.isolate_local_http`）。

    **实测踩到的坑**：`spoolkit.llm.llamacpp` 建 httpx 客户端时曾经没关
    `trust_env`；环境里一旦出现 `HTTP_PROXY=http://127.0.0.1:7890`（Clash 那条），
    它就会把 `/tokenize` 也送去代理，代理对 `127.0.0.1` 的目标回 **502**——整批跑
    从那一刻起全部「未完成（tokenize 返回 502）」。这不是服务挂了（`/props` 与
    `/v1/chat/completions` 都正常），是**本机回环被代理劫走**。

    现在两件事都做：仓库代码里 12 个构造点全部 `trust_env=False`（由
    `envcheck.audit_httpx_trust_env` 守着），跑分器再把这一次的环境摘干净，
    并把结果写进读数（`env`）。
    """
    return envcheck.isolate_local_http()


#: 这一批跑分所处的环境（开跑前自证的那一份）。读数必须能回答「这是在 rg 齐不齐、
#: 代理干不干净的环境里取的」——上一轮 A 类格子正是在没有 rg 的环境里跑出假 0/3。
BATCH_ENV: dict = {}


def _prepare_process_env() -> None:
    """把**当前进程**的环境弄齐、弄干净，并自证一次；不合格就退出码 3。

    对照是**在进程内**装配循环跑的（不是子进程），所以 rg 必须在当前进程的
    PATH 里——上一轮漏的正是这一处：只在子进程里补了 PATH。
    """
    added = envcheck.ensure_probe_tools(os.environ, ("rg", "node"))
    removed = envcheck.isolate_local_http()
    report = envcheck.inspect(
        os.environ, window=int(os.environ.get("ORCH_WINDOW", "8192"))
    )
    say(
        f"环境自证：{'合格' if report.ok else '不合格'}"
        f"；补 PATH={added or '无需补'}"
        f"；摘掉代理变量={sorted(removed) or '无'}"
    )
    if not report.ok:
        say(envcheck.format_report(report))
        say(envcheck.fix_hint(report))
        raise SystemExit(envcheck.EXIT_ENVIRONMENT_BLOCKED)
    BATCH_ENV.clear()
    BATCH_ENV.update(report.to_dict())


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
    _prepare_process_env()
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
    say(f"沙箱 : 每次跑前重摆（现状 {state['project']}，失败测试 exit={state['fail_exit']}）")

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
                    # 每条读数自带环境：合并多批读数时，"这一格是在什么环境里
                    # 取的"必须跟着这一格走，不能只写在文件根上。
                    record["env"] = dict(BATCH_ENV)
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
    # 靶判词留到最后：中途的判词会被后来补上的格子推翻（跑批是边跑边落盘的）。
    for task, verdict in target_verdicts(readings).items():
        arms = "、".join(
            f"{arm} {one['pass']}/{one['of']}"
            for arm, one in (verdict.get("pass_by_arm") or {}).items()
        )
        line = f"靶判词 {task}（{verdict['role']}）：{verdict['verdict']}"
        if arms:
            line += f"；各臂通过 {arms}"
        if verdict.get("next"):
            line += f" —— {verdict['next']}"
        say(line)
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
                cell: dict = {
                    "runs": len(rows),
                    "finished": sum(1 for one in rows if one["finished"]),
                    # 角色跟着格子走：控制靶的格子不能拿来当能力证据。
                    "role": TARGET_ROLES.get(task, "ability"),
                    "passed": sum(1 for one in rows if _cell_passed(one)),
                }
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
        "target_roles": dict(TARGET_ROLES),
        "target_verdicts": target_verdicts(merged),
        "local_http_isolated": True,
        "no_proxy": os.environ.get("NO_PROXY"),
        "env": dict(BATCH_ENV),
        "checks_definition": {
            "A": list(CHECKS["A"]),
            "B": list(CHECKS["B"]),
            "classify_markers": list(CHECK_CLASSIFY),
            "priority_markers": list(CHECK_PRIORITY),
            "b_paths": list(CHECK_B_PATHS),
            "b_plan_markers": list(CHECK_B_PLAN),
            "b_plan_regex": CHECK_B_PLAN_RE.pattern,
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
