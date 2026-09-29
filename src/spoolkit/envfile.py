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

编码上多做两件事，都是"**我明明填了**"这句抱怨逼出来的：Windows 记事本存
UTF-8 常带 BOM（不处理的话键名会变成 `\\ufeffQQ_AppID`，症状是"文件里明明有
这一行，它说没有"），存"Unicode"则是 UTF-16（照 utf-8 读一个字都解析不出来，
而那种文件最不该被说成"空文件"——那会把人往错方向带）。
"""

from __future__ import annotations

from pathlib import Path


def load_env_file(path: Path) -> dict[str, str]:
    """读最简单的 `KEY=VALUE` 形式。不引入第三方依赖，读坏了也不抛。

    读不出来（不存在 / 是目录 / 没权限 / 编码不对）一律返回空字典：密钥是增强，
    该说"没找到"，而不是让命令崩在启动路径上。
    """
    values: dict[str, str] = {}
    try:
        raw = path.read_bytes()
    except OSError:
        return values
    # UTF-16：记事本存"Unicode"的那种。靠 BOM 认，认不出再退回 UTF-8。
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            text = raw.decode("utf-16")
        except UnicodeDecodeError:
            return values
    else:
        try:
            text = raw.decode("utf-8-sig")  # utf-8-sig 顺手吃掉 UTF-8 BOM
        except UnicodeDecodeError:
            return values
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values
