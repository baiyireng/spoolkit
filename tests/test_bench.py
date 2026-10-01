import json
import os
import shutil
from pathlib import Path

import pytest

from spoolkit import bench
from spoolkit.bench import (
    Task,
    TaskResult,
    load_tasks,
    prepare,
    render_report,
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
