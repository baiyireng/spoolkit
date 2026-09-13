"""通道插件装载：核心不认识任何一家通道。

这是从 OpenClaw 那套里抄来的**唯一一件真正重要的东西**：它的核心仓库一行
微信代码都没有，通道是外部插件（npm 包 + manifest），由 Gateway 装载。
好处很实在——接一家新通道不必改核心，也不必把它的依赖塞进主包。

两条装载途径，覆盖两种情形：

1. **装好的包**：entry point 组 `agents_dev.channels`（`pyproject.toml` 里声明
   `[project.entry-points."agents_dev.channels"]`）。适合正经发布出去的适配器。
2. **没打包的脚本**：环境变量 `AGENTS_DEV_CHANNEL_PLUGINS` 给一串
   `模块名` 或 `模块名:工厂函数`（逗号分隔）。适合自己写一个先跑起来——
   个人微信/QQ 那类第三方 hook 的适配器多半就是这种形态。

工厂签名：`build(spec) -> Channel`，其中 spec 是个名字到字符串的字典
（`--channel` 的名字、`--root`、以及环境里的配置）。**不认识的名字才走这里**，
内置的 fake/telegram/wecom 优先。
"""

from __future__ import annotations

import importlib
import importlib.metadata
import os
from typing import Callable

ENTRY_POINT_GROUP = "agents_dev.channels"
ENV_VAR = "AGENTS_DEV_CHANNEL_PLUGINS"


def load_channel_factories() -> dict[str, Callable[[dict], object]]:
    """收集可用的通道工厂：entry point 先，环境变量后（后者可覆盖）。"""
    factories: dict[str, Callable[[dict], object]] = {}
    factories.update(_from_entry_points())
    factories.update(_from_env(os.environ.get(ENV_VAR, "")))
    return factories


def _from_entry_points() -> dict[str, Callable[[dict], object]]:
    found: dict[str, Callable[[dict], object]] = {}
    try:
        entries = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
    except Exception:  # pragma: no cover - 元数据服务不可用时不该挡启动
        return found
    for entry in entries:
        try:
            found[entry.name] = _as_factory(entry.load())
        except Exception:  # pragma: no cover - 坏的插件跳过，不拖垮其它的
            continue
    return found


def _from_env(raw: str) -> dict[str, Callable[[dict], object]]:
    found: dict[str, Callable[[dict], object]] = {}
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        module_name, _, attribute = item.partition(":")
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        target = getattr(module, attribute, None) if attribute else module
        if target is None:
            continue
        factory = _as_factory(target)
        # 名字取**模块名最后一段**：`my_wechat:build` → `my_wechat`。
        # 用函数名当通道名是错的——`--channel build` 没人看得懂；
        # 一个模块要提供多个通道时，用 entry point 那条路显式命名。
        name = module_name.rsplit(".", 1)[-1]
        found[name] = factory
    return found


def _as_factory(target) -> Callable[[dict], object]:
    """统一成 `build(spec)`：

    - 模块或类里有 `build`：直接用；
    - 类本身：用 `类(spec)` 不行，就 `类()`；
    - 函数：直接用。
    插件作者只需要给一个能造出 Channel 的东西，形态不挑。
    """
    if hasattr(target, "build") and callable(getattr(target, "build")):
        return target.build
    if isinstance(target, type):
        return lambda spec: target()
    return target
