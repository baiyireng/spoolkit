"""`.env` 文件的读与写：**三层配置里"密钥"那一层的共同底座**。

为什么要单独一个模块：需要读写 `.env` 的有三处——用户级配置（`settings`）、
通道凭据（`bridge/credentials`）、模型供应商（`llm/providers`），再加上 `spool env`
这条命令。原先这个函数住在 `llm/gemini.py` 里，于是"只想读一个 `.env`"要先把
LLM 网关那一层（连带 `httpx`）import 进来；反过来让 `settings` 去依赖 `llm`
也不成体统。

规矩只有三条：

1. **只有 `KEY=VALUE`**（`export KEY=VALUE` 也认——那是最常见的 shell 写法），
   `#` 开头是注释，值两边的引号去掉；
2. **不做变量展开**（`${OTHER}` 这种）：展开会让"值到底是什么"要读两处才知道，
   而这个项目的偏好是"看得见"；
3. **文件不存在返回空**——配置是增强，不该挡住启动。

编码与换行上都多做了一件事，而且**读与写共用同一个判断**：

- Windows 记事本存 UTF-8 常带 BOM（不处理的话键名会变成 `\\ufeffQQ_AppID`，
  症状是"文件里明明有这一行，它说没有"），存"Unicode"则是 UTF-16（照 utf-8 读
  一个字都解析不出来，而那种文件最不该被说成"空文件"——那会把人往错方向带）。
- **写回时要还原原文的编码与换行**。这条是评审逮出来的：只读时 UTF-16/BOM 都能读，
  一 `set` 就崩或者悄悄追加一份重复键——"看着能用、一改就炸"比干脆不支持更坏。
  CRLF 同理：整份改写成 LF，在 git 里就是"整个文件都改了"。
"""

from __future__ import annotations

from pathlib import Path


class EnvFileError(RuntimeError):
    """`.env` 读写不了时给**人看的一句话**（不带回溯）。"""


def decode_env(raw: bytes) -> tuple[str, str]:
    """按 BOM 判编码，返回（文本, 编码名）。

    编码名是**能原样写回**的那个：`utf-8-sig` 会在写出时把 BOM 加回去，
    `utf-16` 同理——"我改了两个字，git 却显示整文件都动了"就是这么来的。
    """
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16"), "utf-16"
    if raw[:3] == b"\xef\xbb\xbf":
        return raw.decode("utf-8-sig"), "utf-8-sig"
    return raw.decode("utf-8"), "utf-8"


def read_env_text(path: Path) -> tuple[str, str, str]:
    """读 `.env` 原文，返回（文本, 编码, 换行符）。后两个用于**原样写回**。"""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise EnvFileError(f"读不了 {path}：{exc}") from exc
    try:
        text, encoding = decode_env(raw)
    except UnicodeDecodeError as exc:
        raise EnvFileError(
            f"{path} 不是能认出的文本编码（只认 UTF-8 与 UTF-16）：{exc}"
        ) from exc
    newline = "\r\n" if "\r\n" in text else "\n"
    return text, encoding, newline


def iter_env_items(text: str):
    """逐行给出（键, 值）。认 `export ` 前缀，跳过注释与空行。"""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        yield key.strip(), value.strip().strip("'\"")


def load_env_file(path: Path) -> dict[str, str]:
    """读最简单的 `KEY=VALUE` 形式。不引入第三方依赖，读坏了也不抛。

    读不出来（不存在 / 是目录 / 没权限 / 编码不对）一律返回空字典：密钥是增强，
    该说"没找到"，而不是让命令崩在启动路径上。**要改这份文件就别用它**——
    用 `read_env_text`，那里会把编码与换行一起给你，好原样写回。
    """
    try:
        text, _, _ = read_env_text(path)
    except EnvFileError:
        return {}
    return dict(iter_env_items(text))
