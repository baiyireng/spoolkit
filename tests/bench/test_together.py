"""第二条仪器：整批铺进一个工作区，全部交给一条会话。

这里只测**摆盘与验收**那部分（不跑模型）：题目铺得对不对、验收是不是
还走「原始测试覆盖回来」那条路。长任务本身要靠真实运行去量。
"""

import json
from pathlib import Path

from agents_dev.bench import (
    Task,
    load_tasks,
    prepare_together,
    verify_together,
)


def _make_task(root: Path, name: str, *, correct: bool) -> Task:
    home = root / name
    (home / "project").mkdir(parents=True)
    (home / "acceptance").mkdir(parents=True)
    (home / "project" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n" if not correct else "def add(a, b):\n    return a + b\n",
        encoding="utf-8",
    )
    (home / "acceptance" / "test_acceptance.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 4\n",
        encoding="utf-8",
    )
    (home / "task.json").write_text(
        json.dumps(
            {
                "goal": "让 add 通过测试",
                "acceptance": {
                    "command": ["python", "-m", "pytest", "-q"],
                    "tests": ["test_acceptance.py"],
                },
                "scope": ["**"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return Task(
        name=name,
        goal="让 add 通过测试",
        command=("python", "-m", "pytest", "-q"),
        acceptance_files=("test_acceptance.py",),
        scope=("**",),
        home=home,
    )


def test_铺盘_一题一个目录且题目说明在里面(tmp_path: Path) -> None:
    tasks = [_make_task(tmp_path / "tasks", "01_a", correct=False)]
    workspace = tmp_path / "ws"
    prepare_together(tasks, workspace)

    home = workspace / "01_a"
    assert (home / "calc.py").exists()  # 起始代码
    assert (home / "test_acceptance.py").exists()  # 验收测试在里面（要能自己跑）
    text = (home / "TASK.md").read_text(encoding="utf-8")
    assert "让 add 通过测试" in text
    assert "python -m pytest -q" in text
    # 刻意不给总清单：目录里有什么要它自己去列
    assert not (workspace / "README.md").exists()


def test_验收覆盖回原始测试(tmp_path: Path) -> None:
    """agent 改验收测试想蒙混过关的话，覆盖回来就作废——这条机制不能破。"""
    tasks = [_make_task(tmp_path / "tasks", "01_a", correct=False)]
    workspace = tmp_path / "ws"
    prepare_together(tasks, workspace)
    home = workspace / "01_a"

    # 先把代码改对，再把验收测试改成永远通过——验收时测试会被覆盖回去，
    # 于是结果只反映它对被测代码做了什么。
    (home / "calc.py").write_text("def add(a, b):\n    return 4\n", encoding="utf-8")
    (home / "test_acceptance.py").write_text(
        "def test_nothing():\n    assert True\n", encoding="utf-8"
    )
    results = verify_together(tasks, workspace)
    assert results[0].passed is True

    # 反过来：改测试不改代码，必须判失败
    workspace2 = tmp_path / "ws2"
    prepare_together(tasks, workspace2)
    (workspace2 / "01_a" / "test_acceptance.py").write_text(
        "def test_nothing():\n    assert True\n", encoding="utf-8"
    )
    assert verify_together(tasks, workspace2)[0].passed is False


def test_真实的题集能读出来并铺好(tmp_path: Path) -> None:
    """用仓库里那 50 道真题走一遍摆盘，防止题集结构变了这里不知道。"""
    tasks = load_tasks(Path("benchmarks/tasks"))
    assert len(tasks) == 50
    workspace = tmp_path / "ws"
    prepare_together(tasks, workspace)
    assert len([p for p in workspace.iterdir() if p.is_dir()]) == 50
    first = workspace / tasks[0].name
    assert (first / "TASK.md").exists()
    assert list(first.glob("test_acceptance.py"))
