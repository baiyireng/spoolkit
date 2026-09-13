"""通道凭据从哪来：**环境变量 → 项目 `.env`**，并且要把来源说出来。

为什么要有这一层：用户把 QQ 的 AppID/AppSecret 写进了项目根的 `.env`
（那里本来就是放密钥的地方，也已经被 .gitignore 排除），而通道适配器原先只认
进程环境变量——于是"我明明填了"变成一句没有办法排查的抱怨。

三条规矩：

1. **来源要说出来**：查到了要说清是从环境变量还是从哪个文件来的。
2. **容错大小写与别名**：`QQ_AppID` / `QQ_APPID` / `AGENTS_DEV_QQ_APPID`
   都认——用户已经按自己的习惯写下了，让人去改拼写是最没必要的摩擦。
3. **值不回显**：日志与报错只给前几位 + `***`，密钥不进日志。
"""

from __future__ import annotations

import os
from pathlib import Path

from spoolkit.llm.gemini import load_env_file


def find(root: Path | None, *names: str) -> tuple[str, str]:
    """按顺序找第一个有值的名字。返回（值, 来源说明）。

    顺序是**环境变量优先于 .env**：环境变量是"这次 shell 的意图"，
    文件是长期设置——和 provider 配置的优先级一致。
    """
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value, f"环境变量 {name}"
    if root is not None:
        env_file = root / ".env"
        table = load_env_file(env_file)
        for name in names:
            value = (table.get(name) or "").strip()
            if value:
                return value, f"{env_file} 里的 {name}"
    return "", "没找到（环境变量或 .env 都没有）"


def mask(value: str) -> str:
    """给日志看的形状：够认出是哪一份，又不至于泄露。"""
    if not value:
        return "（空）"
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:3]}***{value[-2:]}"


# 每个通道认哪些名字（前者是文档里的规范名）。`AGENTS_DEV_*` 是改名前的旧名，
# 留着是为了别人 .env 里已经写好、脚本里已经 export 的那一份不失效。
QQ_APP_ID = ("SPOOLKIT_QQ_APPID", "AGENTS_DEV_QQ_APPID", "QQ_AppID", "QQ_APPID")
QQ_SECRET = ("SPOOLKIT_QQ_SECRET", "AGENTS_DEV_QQ_SECRET", "QQ_AppSecret", "QQ_SECRET")
TELEGRAM_TOKEN = (
    "SPOOLKIT_TELEGRAM_TOKEN",
    "AGENTS_DEV_TELEGRAM_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_TOKEN",
)
WECOM_CORP_ID = ("SPOOLKIT_WECOM_CORP_ID", "AGENTS_DEV_WECOM_CORP_ID", "WECOM_CORP_ID")
WECOM_SECRET = ("SPOOLKIT_WECOM_SECRET", "AGENTS_DEV_WECOM_SECRET", "WECOM_SECRET")
WECOM_AGENT_ID = (
    "SPOOLKIT_WECOM_AGENT_ID",
    "AGENTS_DEV_WECOM_AGENT_ID",
    "WECOM_AGENT_ID",
)
