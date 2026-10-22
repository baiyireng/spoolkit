"""`@pytest.mark.needs("能力名")`：把"这个用例依赖什么宿主能力"写成声明。

为什么要有它：受限沙箱下，有些用例红的**原因不是代码**——比如
`tests/cli/test_service_command.py` 那几条要按进程树杀进程，而沙箱拒绝
`taskkill`。以前它们就是红的，读到的人先怀疑自己的改动，再花时间排除环境
（这一轮就花了）。写成声明之后：**缺能力 → 跳过并说明缺什么**；能力在 →
照常跑，红了就是真回归——不会因为"环境"两个字把真缺陷也放过。

能力名与探测逻辑在 `scripts/dev_env.py`（单一事实来源），这里只做接线。
"""

from __future__ import annotations

from typing import Any

import pytest

import dev_env

_CACHE: dict[str, tuple[bool, str]] = {}


def _capability(name: str) -> tuple[bool, str, str]:
    """探测一次就缓存：体检要起进程，不能每个用例都重来一遍。

    返回 `(可用?, 细节, 受限下的替代做法)`——**跳过的理由里要带上"所以呢"**：
    读到"缺 process_kill"的人下一个问题必然是"那我该干什么"。
    """
    if name not in _CACHE:
        caps = dev_env.probe_capabilities()
        item = caps.get(name)
        if item is None:
            _CACHE[name] = (False, f"未知能力：{name}（去 scripts/dev_env.py 登记）", "")
        else:
            info = dev_env.CAPABILITIES.get(name)
            _CACHE[name] = (
                bool(item["ok"]),
                str(item["detail"]),
                info.workaround if info else "",
            )
    return _CACHE[name]


def pytest_configure(config: Any) -> None:
    config.addinivalue_line(
        "markers",
        "needs(capability): 这条用例依赖某个宿主能力；缺能力时跳过并说明原因",
    )


def pytest_collection_modifyitems(config: Any, items: list[Any]) -> None:
    for item in items:
        for mark in item.iter_markers(name="needs"):
            name = str(mark.args[0]) if mark.args else ""
            ok, detail, workaround = _capability(name)
            if not ok:
                reason = (
                    f"环境缺能力：{name}（{detail}）——这不是回归；在正常环境里它必须绿"
                )
                if workaround:
                    reason += f"\n    受限下这么做：{workaround}"
                item.add_marker(pytest.mark.skip(reason=reason))
            break
