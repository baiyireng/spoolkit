"""回归集的完整性。

一个在未修改状态下就通过验收的任务毫无意义：它只会虚高分数，并且把
「模型什么都没做」伪装成「模型做对了」。所以每个任务的原始代码都必须
让验收失败——这条比任务本身写了什么更要紧。
"""

import tempfile
from pathlib import Path

import pytest

from agents_dev.bench import load_tasks, prepare, verify

ROOT = Path(__file__).resolve().parents[2]


def _tasks():
    return load_tasks(ROOT / "benchmarks" / "tasks")


def test_任务集规模够用() -> None:
    """样本太少时，一次改动的效果和噪声分不开。"""
    assert len(_tasks()) >= 20


def test_任务名不重复() -> None:
    names = [task.name for task in _tasks()]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("task", _tasks(), ids=lambda task: task.name)
def test_原始状态验收失败(task) -> None:
    with tempfile.TemporaryDirectory(
        prefix="integrity-", ignore_cleanup_errors=True
    ) as temp:
        workspace = Path(temp) / "project"
        prepare(task, workspace)
        passed, detail = verify(task, workspace)
    assert not passed, f"{task.name} 未修改就通过验收：{detail}"
