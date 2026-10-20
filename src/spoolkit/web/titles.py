"""会话标题的 Agent 生成器。

左栏要显示"人能读的名字"，但会话 id 可以是 uuid / 时间戳。最可靠的名字来自
**跑完一轮之后**让同一个模型看一眼目标与结论——那时模型已经空闲，不会再和
正在跑的任务抢 llama.cpp 的单槽。生成失败不影响任何事：调用方会保留
`kernel_sessions.ensure_title` 给的种子标题。

这一层只做三件事：
1. 从网页壳已经拿到的 `extra_args` / 用户配置里装载模型（没有就返回 None）；
2. 发一条很小的命名请求；
3. 把模型回复清洗成一行标题。

它**不**写盘、不改状态：写 meta 与推 `/state` 由 `WebState` 决定。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence

from spoolkit import settings
from spoolkit.kernel.sessions import clean_title
from spoolkit.llm.providers import ProviderConfig, load_gateway, provider_names
from spoolkit.llm.types import ChatRequest, Message
from spoolkit.net import system_proxy

TITLE_MAX_TOKENS = 48
TITLE_TIMEOUT_SECONDS = 12.0

SYSTEM = (
    "你是一个会话命名器。你只输出一个简短标题，"
    "不要解释，不要引号，不要编号，不要标点结尾。"
)

PROMPT = (
    "下面是一条 coding agent 会话的用户目标与助手结论。\n"
    "请给这条会话起一个 4-18 个字的中文标题：要具体、可读，"
    "能让人一眼认出这次做了什么；不要用“会话”“任务”“代码修改”这类空词。\n"
    "只输出标题。\n\n"
    "用户目标：\n{goal}\n\n"
    "助手结论（可能不完整）：\n{reply}\n"
)


def _flag(extra_args: Sequence[str], name: str) -> str:
    """从 `--provider llamacpp` 这种参数表里取一个值。"""
    args = list(extra_args)
    for index, item in enumerate(args[:-1]):
        if item == name:
            return str(args[index + 1])
        if item.startswith(name + "="):
            return item.split("=", 1)[1]
    return ""


def _resolve(key: str, extra_args: Sequence[str]) -> str:
    value = _flag(extra_args, "--" + key.replace("_", "-"))
    if value:
        return value
    value, _source = settings.resolve(key, "")
    return str(value or "")


class TitleNamer:
    """按网页壳的模型配置发一次命名请求。

    `gateway` 可注入：测试与嵌入方不需要真的起一个 llama-server。
    """

    def __init__(
        self,
        project_root: Path | str,
        extra_args: Sequence[str] = (),
        *,
        gateway: object | None = None,
        timeout: float = TITLE_TIMEOUT_SECONDS,
    ) -> None:
        self.project_root = Path(project_root)
        self.extra_args = tuple(extra_args)
        self._gateway = gateway
        self._timeout = float(timeout)
        self._load_failed = False

    def _load_gateway(self) -> object | None:
        if self._gateway is not None:
            return self._gateway
        if self._load_failed:
            return None
        provider = _resolve("provider", self.extra_args) or "fake"
        if provider not in provider_names() or provider == "fake":
            # 假模型只回放脚本，不能命名；宁可直接用种子标题。
            self._load_failed = True
            return None
        try:
            config = ProviderConfig(
                project_root=self.project_root,
                model=_resolve("model", self.extra_args),
                base_url=_resolve("base_url", self.extra_args),
                proxy=_resolve("proxy", self.extra_args) or system_proxy(),
                timeout=self._timeout,
                env=dict(os.environ),
                temperature=0.0,
            )
            self._gateway = load_gateway(provider, config)
        except Exception:  # noqa: BLE001 - 标题是锦上添花
            self._load_failed = True
            self._gateway = None
        return self._gateway

    def generate(self, goal: str, reply: str = "") -> str:
        """返回清洗后的标题；没有模型 / 调用失败 / 空回复时返回空串。"""
        gateway = self._load_gateway()
        if gateway is None:
            return ""
        text = str(goal or "").strip()
        if not text:
            return ""
        prompt = PROMPT.format(goal=text[:1200], reply=str(reply or "").strip()[:1600])
        try:
            response = gateway.chat(
                ChatRequest(
                    messages=(
                        Message(role="system", content=SYSTEM),
                        Message(role="user", content=prompt),
                    ),
                    max_tokens=TITLE_MAX_TOKENS,
                )
            )
        except Exception:  # noqa: BLE001 - 模型超时/断线不该弹错
            return ""
        return clean_title(getattr(response, "text", "") or "")
