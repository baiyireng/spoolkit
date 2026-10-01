import argparse
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from spoolkit import bench
from spoolkit.bench import (
    Task,
    TaskResult,
    load_tasks,
    prepare,
    render_report,
    render_together,
    run_task,
    verify,
)
from spoolkit.bench import DEFAULT_BENCH_ROOT

REPO_ROOT = Path(__file__).resolve().parent.parent
BENCH_ROOT = REPO_ROOT / DEFAULT_BENCH_ROOT


def test_读取到全部任务() -> None:
    tasks = load_tasks(BENCH_ROOT)
    assert len(tasks) >= 10
    assert all(task.goal for task in tasks)
    assert all(task.command for task in tasks)


def test_任务名不重复() -> None:
    names = [task.name for task in load_tasks(BENCH_ROOT)]
    assert len(names) == len(set(names))


def test_起始状态下每个任务都验收失败(tmp_path: Path) -> None:
    """这条是任务集自身的体检。

    一个在起始状态就能通过的任务毫无意义——它测不出任何东西。
    而且这种错误很隐蔽：报告上显示「通过」，你还以为 agent 做对了。
    """
    failures = []
    for task in load_tasks(BENCH_ROOT):
        workspace = tmp_path / task.name
        prepare(task, workspace)
        passed, _ = verify(task, workspace)
        if passed:
            failures.append(task.name)
    assert not failures, f"这些任务不做任何改动就能通过验收：{failures}"


def test_验收测试放进工作区供自验(tmp_path: Path) -> None:
    """agent 必须能自己跑测试——「改完能知道对不对」是核心能力。"""
    for task in load_tasks(BENCH_ROOT):
        workspace = tmp_path / task.name
        prepare(task, workspace)
        for name in task.acceptance_files:
            assert (workspace / name).exists(), f"{task.name} 没提供验收测试"


def test_验收时会还原被改动的测试(tmp_path: Path) -> None:
    """改测试让测试通过是作弊；验收前用原始副本覆盖，作弊就无效。"""
    task = load_tasks(BENCH_ROOT)[0]
    workspace = tmp_path / "w"
    prepare(task, workspace)

    for name in task.acceptance_files:
        (workspace / name).write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    # 原始测试要求真的修好代码，还原后必然失败
    passed, _ = verify(task, workspace)
    assert passed is False


def test_还原后测试内容与原始一致(tmp_path: Path) -> None:
    task = load_tasks(BENCH_ROOT)[0]
    workspace = tmp_path / "w"
    prepare(task, workspace)
    name = task.acceptance_files[0]
    (workspace / name).write_text("被改了", encoding="utf-8")
    verify(task, workspace)
    assert (workspace / name).read_text(encoding="utf-8") == (
        task.home / "acceptance" / name
    ).read_text(encoding="utf-8")


def test_准备阶段复制了起始代码(tmp_path: Path) -> None:
    task = load_tasks(BENCH_ROOT)[0]
    workspace = tmp_path / "w"
    prepare(task, workspace)
    assert any(workspace.iterdir())


def test_验收会替换掉文件时输出被截断(tmp_path: Path) -> None:
    task = Task(
        name="t",
        goal="g",
        command=("python", "-c", "print('x' * 5000)"),
    )
    passed, detail = verify(task, tmp_path)
    assert passed is True
    assert len(detail) <= 600


def test_缺少验收命令时判为失败(tmp_path: Path) -> None:
    task = Task(name="t", goal="g", command=())
    passed, detail = verify(task, tmp_path)
    assert passed is False
    assert "验收命令" in detail


def _fake_loop_builder(workspace: Path):
    class _Loop:
        def run(self, goal, resume=False):
            class _Result:
                finished = True
                steps = 3
                prompt_tokens = 1200
                model_calls = 3

            # 不动任何代码：验收必然失败
            return _Result()

    return _Loop()


def test_跑一个任务并记录结果_build(tmp_path: Path) -> None:
    task = load_tasks(BENCH_ROOT)[0]
    result = run_task(task, _fake_loop_builder)
    assert isinstance(result, TaskResult)
    assert result.name == task.name
    assert result.agent_finished is True
    # agent 自称完成但没真改，验收必须失败
    assert result.passed is False


def test_报告包含完成率与平均指标() -> None:
    results = [
        TaskResult("a", True, True, 3, 1000, 3, 1.0),
        TaskResult("b", False, True, 5, 2000, 5, 2.0),
    ]
    text = render_report(results)
    assert "验收通过 1/2" in text
    assert "平均步数 4.0" in text
    assert "平均输入 1500 token" in text


def test_报告点出自称完成却失败的任务() -> None:
    results = [
        TaskResult("a", True, True, 3, 1000, 3, 1.0),
        TaskResult("b", False, True, 5, 2000, 5, 2.0),
    ]
    text = render_report(results)
    assert "自称完成但验收失败" in text
    assert "b" in text


def test_空结果有明确提示() -> None:
    assert "没有任务" in render_report([])


def test_任务描述与验收命令来自文件(tmp_path: Path) -> None:
    home = tmp_path / "x"
    (home / "project").mkdir(parents=True)
    (home / "acceptance").mkdir()
    (home / "acceptance" / "t.py").write_text("", encoding="utf-8")
    (home / "task.json").write_text(
        json.dumps(
            {
                "goal": "做点事",
                "acceptance": {"command": ["python", "-m", "pytest"], "tests": ["t.py"]},
                "scope": ["src"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    task = load_tasks(tmp_path)[0]
    assert task.goal == "做点事"
    assert task.command == ("python", "-m", "pytest")
    assert task.acceptance_files == ("t.py",)
    assert task.scope == ("src",)


def test_任务工作区建出来就能用(raw_dir_creation) -> None:
    """建出来的工作区必须当场可遍历、可写、可往下 copytree。

    受限环境（Windows + 文件沙箱）里，`mode=0o700` 建出来的目录连
    `os.scandir` 都 WinError 5，事后 `chmod` 也救不回来——所以只能创建那一刻抬。
    改回 `tempfile.TemporaryDirectory(...)` 裸用，这里的 `os.listdir` 就是红的。

    `raw_dir_creation` 先把测试插件那层抬 mode 的补丁摘掉：真实 CLI 里没有它，
    bench 得自己抬（不摘的话，改回旧写法也照样绿）。
    """
    with bench.temporary_workspace() as root:
        assert os.listdir(root) == []

        source = root / "source"
        (source / "sub" / "deep").mkdir(parents=True)
        (source / "a.txt").write_text("a", encoding="utf-8")
        (source / "sub" / "deep" / "b.txt").write_text("b", encoding="utf-8")

        workdir = root / "project"
        shutil.copytree(source, workdir)
        (workdir / "新文件.txt").write_text("写进去了", encoding="utf-8")

        assert sorted(os.listdir(workdir)) == ["a.txt", "sub", "新文件.txt"]
        assert (workdir / "sub" / "deep" / "b.txt").read_text(encoding="utf-8") == "b"


def test_工作区里碰不得的子目录不影响收尾(raw_dir_creation) -> None:
    """验收跑过 pytest 之后，工作区里会留下它自己用 0o700 建的垃圾目录。

    收尾必须真的「忽略清理错误」——否则任务全跑完了、结果却在收尾时抛掉，
    人看到的是一场空。实测 `TemporaryDirectory(ignore_cleanup_errors=True)`
    在本沙箱里**照样抛**（它的处理器会去 `chmod` 那个目录，而 `chmod` 也被拒），
    所以这里必须是 `shutil.rmtree(..., ignore_errors=True)`。
    """
    with bench.temporary_workspace() as root:
        poisoned = root / "poisoned"
        os.mkdir(poisoned, 0o700)  # 走未被抬 mode 的那一份
        try:
            os.listdir(poisoned)
        except PermissionError:
            # 本沙箱：0o700 的目录确实碰不得，收尾时正好会踩到它。
            pass
    # 能走到这里 = 退出 with 时没抛，这正是要钉的那条。


def test_权限被环境限制时给人话(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """抬不起权限时的兜底：说清是**环境**限制了**目录权限**、并指出**路径**。

    钉的是要点，不是整句——原话以后可以改，但这三样不能说没就没
    （现在的表现是裸 traceback 糊到人脸上，正是这条要挡住的）。
    """
    home = tmp_path / "task"
    (home / "project").mkdir(parents=True)
    (home / "project" / "a.py").write_text("x = 1\n", encoding="utf-8")
    task = Task(name="t", goal="g", command=("python", "-c", "pass"), home=home)

    def _denied(*args, **kwargs):
        raise PermissionError(5, "拒绝访问。")

    monkeypatch.setattr(bench.shutil, "copytree", _denied)
    workdir = tmp_path / "w"
    with pytest.raises(bench.WorkspaceUnavailableError) as caught:
        bench.prepare(task, workdir)

    message = str(caught.value)
    assert "环境" in message
    assert "权限" in message
    assert str(workdir) in message


# --- 验收解释器：选错一次，整批结论全反 -------------------------------------
#
# 真实的翻车：`spool` 装在 uv tool 里时，验收一律拿 `sys.executable`（那个环境的
# 解释器）跑，于是每道题都死于 `No module named pytest`，报告长成「验收 0/50」；
# 同一个任务用仓库 `.venv` 跑是 50/50。选择顺序、开跑前自检、输出转换三条，
# 缺一条这种误判就能回来。


def _fake_venv(root: Path) -> Path:
    """造一个假虚拟环境解释器（只要求文件在；选择逻辑不看它能不能跑）。"""
    venv_dir = root / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    venv_dir.mkdir(parents=True)
    fake = venv_dir / ("python.exe" if os.name == "nt" else "python")
    fake.write_text("", encoding="utf-8")
    return fake


def _isolated_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把「检出目录」指到 tmp 里，免得测试结果取决于这台机器上的真实 .venv。"""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(bench, "spoolkit_checkout_root", lambda: checkout)
    return checkout


def test_验收解释器优先用任务工作区的虚拟环境(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """① 题目自带 `.venv` 时用它——那才是这道题的环境。

    证伪：把候选顺序改成「检出目录优先」或「永远 sys.executable」，这条就红。
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    checkout = _isolated_checkout(tmp_path, monkeypatch)
    mine = _fake_venv(workspace)
    _fake_venv(checkout)

    assert bench.acceptance_interpreter(workspace) == str(mine)


def test_工作区没有虚拟环境时用检出目录的(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """② 题目不带 `.venv` 时用 spoolkit 检出目录的——uv tool 装的那条路靠它。

    证伪：改回「永远 sys.executable」就红（这里选的必须是检出目录那个路径）。
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    checkout = _isolated_checkout(tmp_path, monkeypatch)
    theirs = _fake_venv(checkout)
    assert str(theirs) != sys.executable

    assert bench.acceptance_interpreter(workspace) == str(theirs)


def test_两处都没有虚拟环境时退回当前解释器(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """③ 一个都没有就退回当前解释器：旧行为，任何环境下都还能用。

    证伪：把兜底改成「没有 .venv 就抛异常/返回固定路径」，这条就红。
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _isolated_checkout(tmp_path, monkeypatch)

    assert bench.acceptance_interpreter(workspace) == sys.executable


def test_验收命令按选中的解释器换算(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """映射本身不变，只换解释器：`pytest` → `<解释器> -m pytest`，`python` → 解释器。

    这一条同时钉住「verify 真的用了选中的解释器」——只改选择函数不改 verify，
    这里照样红。
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _isolated_checkout(tmp_path, monkeypatch)
    mine = _fake_venv(workspace)

    seen: list[list[str]] = []

    class _Done:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def _run(command, **kwargs):
        seen.append(list(command))
        return _Done()

    monkeypatch.setattr(bench.subprocess, "run", _run)

    passed, _ = verify(Task(name="t", goal="g", command=("pytest", "-q")), workspace)
    assert passed is True
    passed, _ = verify(
        Task(name="t", goal="g", command=("python", "-m", "pytest", "-q")), workspace
    )
    assert passed is True

    assert seen == [
        [str(mine), "-m", "pytest", "-q"],
        [str(mine), "-m", "pytest", "-q"],
    ]


def test_缺pytest时的说明含解释器路径与出路(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """自检的判据与说明：任务要 pytest + 解释器跑不了 → 阻塞，且说清是谁的问题。

    钉要点（不钉整句）：解释器路径、pytest、不是模型。
    """
    workspace = tmp_path / "task" / "project"
    workspace.mkdir(parents=True)
    _isolated_checkout(tmp_path, monkeypatch)
    mine = _fake_venv(workspace)

    monkeypatch.setattr(
        bench, "probe_pytest", lambda interpreter: (False, "No module named pytest")
    )
    task = Task(
        name="a",
        goal="g",
        command=("python", "-m", "pytest", "-q"),
        home=workspace.parent,
    )
    environment = bench.check_acceptance_environment([task])

    assert environment.needs_pytest is True
    assert environment.blocked is True
    assert environment.missing_pytest == (str(mine),)

    message = bench.describe_environment_problem(environment)
    assert str(mine) in message
    assert "pytest" in message
    assert "不是模型" in message


def test_只用python的任务不被缺pytest拦住(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """容错那一半：任务只用 python 时，解释器里没有 pytest 不该拦整批。"""
    _isolated_checkout(tmp_path, monkeypatch)
    monkeypatch.setattr(
        bench, "probe_pytest", lambda interpreter: (False, "No module named pytest")
    )
    task = Task(name="a", goal="g", command=("python", "run.py"), home=tmp_path)

    environment = bench.check_acceptance_environment([task])

    assert environment.needs_pytest is False
    assert environment.blocked is False
    assert environment.missing_pytest  # 但缺 pytest 这件事本身是记下来的


def test_探不到pytest时整批在开跑前就停(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """开跑前停下：模型压根不启动，报告里也不会出现「验收 0/N」。

    证伪：把 CLI 里的自检拿掉，这条会真的去跑第一道题（fake 供应商，不联网），
    于是「没有验收报告」这条断言红。
    """
    from spoolkit.cli.commands.bench import bench as bench_command

    _isolated_checkout(tmp_path, monkeypatch)
    monkeypatch.setattr(
        bench, "probe_pytest", lambda interpreter: (False, "No module named pytest")
    )
    args = argparse.Namespace(
        root=str(REPO_ROOT),
        tasks=DEFAULT_BENCH_ROOT,
        filter="",
        limit=1,
        provider="fake",
        together=False,
        verbose=False,
        model="",
        base_url="",
        proxy="",
        window=0,
        max_steps=1,
        step_ceiling=0,
        no_setup=True,
    )

    code = bench_command(args)
    captured = capsys.readouterr()

    assert code == bench.EXIT_ENVIRONMENT_BLOCKED
    assert "pytest" in captured.err
    assert "不是模型" in captured.err
    # 没有开跑：没有验收表，也没有「失败」
    assert "验收通过" not in captured.out
    assert "失败" not in captured.out


def test_验收输出缺pytest时翻成人话(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验收输出里出现 `No module named pytest` 时不许原样当成「验收失败」。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _isolated_checkout(tmp_path, monkeypatch)
    mine = _fake_venv(workspace)

    class _Done:
        returncode = 1
        stdout = ""
        stderr = f"{mine}: No module named pytest\n"

    monkeypatch.setattr(bench.subprocess, "run", lambda *a, **k: _Done())

    passed, detail = verify(
        Task(name="t", goal="g", command=("python", "-m", "pytest", "-q")), workspace
    )

    assert passed is False
    assert str(mine) in detail
    assert "pytest" in detail
    assert "不是模型" in detail
    assert bench.looks_like_missing_pytest(detail) is True


def test_跑任务时缺pytest标成环境阻塞(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一题一会话那条路也要分类：结果里带着 blocked，报告才好单独计数。"""
    task = load_tasks(BENCH_ROOT)[0]

    class _Done:
        returncode = 1
        stdout = ""
        stderr = "No module named pytest"

    monkeypatch.setattr(bench.subprocess, "run", lambda *a, **k: _Done())

    result = run_task(task, _fake_loop_builder)

    assert result.passed is False
    assert result.blocked is True
    assert "不是模型" in result.detail


def test_报告把环境阻塞与模型失败分开() -> None:
    """阻塞单列一档：它没被真的验收过，写「失败」就是把环境问题记在模型头上。"""
    blocked = TaskResult("a", False, True, 3, 1000, 3, 1.0, blocked=True)
    failed = TaskResult("b", False, True, 3, 1000, 3, 1.0)

    text = render_report([blocked, failed])

    assert "环境阻塞 1 个" in text
    assert "不是模型" in text
    # 表里那一行显示「阻塞」，不是「失败」
    assert "a" in text
    # 「自称完成但验收失败」只点 b：a 的验收根本没跑起来，不能算它虚假完成
    overconfident = [line for line in text.splitlines() if "自称完成但验收失败" in line]
    assert overconfident, text
    assert "b" in overconfident[0]
    assert "a" not in overconfident[0]


def test_整批单会话那条路也把环境阻塞单列(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第二条仪器（`--together`）验收走的是同一个 verify，分类也要一致。"""
    home = tmp_path / "01_x"
    (home / "project").mkdir(parents=True)
    (home / "acceptance").mkdir()
    (home / "acceptance" / "t.py").write_text("", encoding="utf-8")
    task = Task(
        name="01_x",
        goal="g",
        command=("python", "-m", "pytest", "-q"),
        acceptance_files=("t.py",),
        home=home,
    )
    workspace = tmp_path / "ws"
    (workspace / "01_x").mkdir(parents=True)

    class _Done:
        returncode = 1
        stdout = ""
        stderr = "No module named pytest"

    monkeypatch.setattr(bench.subprocess, "run", lambda *a, **k: _Done())

    results = bench.verify_together([task], workspace)
    assert results[0].blocked is True

    text = render_together(
        results,
        {
            "steps": 0,
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "seconds": 0.0,
            "workspace": workspace,
        },
    )
    assert "环境阻塞 1 个" in text
    assert "不是模型" in text
    # 阻塞的不进「没过的」
    assert "没过的" not in text

