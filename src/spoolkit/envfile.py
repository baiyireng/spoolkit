"""`.env` 文件的读法：**三层配置里"密钥"那一层的共同底座**。

为什么要单独一个模块：需要读 `.env` 的有三处——用户级配置（`settings`）、
通道凭据（`bridge/credentials`）、模型供应商（`llm/providers`）。原先这个函数
住在 `llm/gemini.py` 里，于是"只想读一个 `.env`"要先把 LLM 网关那一层
（连带 `httpx`）import 进来；反过来让 `settings` 去依赖 `llm` 也不成体统。

规矩只有三条：

1. **只有 `KEY=VALUE`**，`#` 开头是注释，值两边的引号去掉；
2. **不做变量展开**（`${OTHER}` 这种）：展开会让"值到底是什么"要读两处才知道，
   而这个项目的偏好是"看得见"；
3. **文件不存在返回空**——配置是增强，不该挡住启动。
"""

from __future__ import annotations

from pathlib import Path


def load_env_file(path: Path) -> dict[str, str]:
    """读最简单的 `KEY=VALUE` 形式。不引入第三方依赖，读坏了也不抛。"""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # 不存在、没权限、编码不对：都当"没有这份文件"。密钥是增强，
        # 读不出来时该说"没找到"，而不是让整个命令崩在启动路径上。
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values
