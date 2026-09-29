r"""通道凭据从哪来：**环境变量 → 工作区 `.env` → 用户级 `.env`**，并且把来源说出来。

为什么要有这一层：用户把 QQ 的 AppID/AppSecret 写进了 `.env`（那里本来就是放密钥
的地方），而通道适配器原先只认进程环境变量——于是"我明明填了"变成一句没有办法
排查的抱怨。

为什么现在多一层用户级（2026-09-27）：**通道是机器级的服务**。一条
`spool bridge`（不给 `--root`）服务整台机器，"你站在哪个目录"不该决定它读哪份
密钥。真实事故是这个样子的：在 `C:\Users\32615` 敲 `spool bridge --channel qqbot …`，
报"缺少 AppID 与 AppSecret"、并建议写进 `C:\Users\32615\.env`——而凭据好好地在
`D:\workSpace\agentWorkSpace\.env` 里。根因是 `root` 缺省取了 cwd。

四条规矩：

1. **来源要说出来**：查到了要说清是从环境变量、还是从哪个文件来的。
2. **容错大小写与别名**：`QQ_AppID` / `QQ_APPID` / `AGENTS_DEV_QQ_APPID`
   都认——用户已经按自己的习惯写下了，让人去改拼写是最没必要的摩擦。
3. **值不回显**：日志与报错只给前几位 + `***`，密钥不进日志。
4. **不缓存**：桥是长驻进程，凭什么拿"启动那一刻的快照"回答所有后续问题。

解析顺序本身由 `spoolkit.settings.resolve_secret` 统一决定：三个入口
（通道、模型供应商、网页搜索）共用同一条链，不再各写一套。
"""

from __future__ import annotations

from pathlib import Path

from spoolkit import settings


def find(root: Path | None, *names: str) -> tuple[str, str]:
    """按顺序找第一个有值的名字。返回（值, 来源说明）。

    `root` 是**这次运行绑定的工作区**（不是当前目录）：给了就看 `<root>/.env`，
    没给（机器级通道）就只剩用户级那一份。环境变量始终优先——它是"这次 shell
    的意图"，文件是长期设置。
    """
    return settings.resolve_secret(names, workspace=root)


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
