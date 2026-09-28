"""`.env` 文件的**读与写**：三层配置里"密钥"那一层的共同底座。

为什么读与写住在同一个模块：同一个 `.env` 有两条写路径——人敲的 `spool env set`
与第一次跑起来的向导（`onboarding`，写的是**用户级**那份＝这台机器上全部凭据所在
的那一份）——而"读用一套判断、写用另一套"正是最难查的坏法：只读时 UTF-16/BOM 都
读得动，一写就崩，或者悄悄多出一行重复键。所以编码、换行、`export ` 前缀都只有
这一份实现，两边都调它。

为什么它单独成一个模块（原先读函数住在 `llm/gemini.py`）：需要读 `.env` 的有三处
——用户级配置（`settings`）、通道凭据（`bridge/credentials`）、模型供应商
（`llm/providers`），再加上 `spool env` 这条命令。住在 `llm` 里的话，"只想读一个
`.env`"要先把 LLM 网关那一层（连带 `httpx`）import 进来；反过来让 `settings` 去依赖
`llm` 也不成体统。

读的三条规矩：

1. **只有 `KEY=VALUE`**（`export KEY=VALUE` 也认，大小写与空白都容错——那是最常见的
   shell 写法，不认它等于那一行对我们隐形），`#` 开头是注释，值两边的引号去掉；
2. **不做变量展开**（`${OTHER}` 这种）：展开会让"值到底是什么"要读两处才知道，
   而这个项目的偏好是"看得见"；
3. **文件不存在返回空**——配置是增强，不该挡住启动。

读与写都按原文还原**编码与换行**，两条都是"我明明填了"这句抱怨逼出来的：

- Windows 记事本存 UTF-8 常带 BOM（不处理的话键名会变成 `\\ufeffQQ_AppID`，症状是
  "文件里明明有这一行，它说没有"），存"Unicode"则是 UTF-16（照 utf-8 读一个字都
  解析不出来，而那种文件最不该被说成"空文件"——那会把人往错方向带）；
- 写回时 `utf-8-sig` 会把 BOM 加回去、`utf-16` 同理——"我只改了两个字节，git 却显示
  整个文件都动了"就是这么来的；CRLF 同理。

写的一条底线：**原子**。`write_text` / `write_bytes` 是"先截断再写"（实测
`open(path, "wb")` 一调用，文件就已经是 0 字节了），密钥文件若在写入中途失败
（磁盘满、进程被杀、断电）就**不可恢复**，而这份文件常常是一个账号唯一的一份凭据。
所以先写同目录的临时文件、再 `os.replace` 换上去，结果收敛成"要么旧的、要么新的"。
"""

from __future__ import annotations

import contextlib
import os
import re
import sys
import tempfile
import time
from pathlib import Path

# `export` 的三种常见写法（`EXPORT` / `Export` 也认），后面必须是空白：
# `exportKEY=v` 不是导出语句，而是一个键名恰好以 export 开头的变量。
_EXPORT_RE = re.compile(r"^(export[ \t]+)(.*)$", re.IGNORECASE)

# 临时文件的命名：`.env.XXXX.tmp`。**必须紧贴目标名**，因为仓库 `.gitignore` 认的是
# `.env` 与 `.env.*`——名字离远了（例如 `..env.*`）就等于把含明文密钥的文件放进版本库视野。
_TEMP_SUFFIX = ".tmp"

# 多久没动过的临时文件算"陈旧的"。这不是标定值、也不是策略：一次写入是毫秒级，
# 留一小时只是为了**不误删另一个正在写入的进程的临时文件**（同机并发写同一个 .env
# 本来就罕见）。所以它取的是"远大于任何一次写入"的量级，而不是某个调优结果。
_STALE_TEMP_SECONDS = 3600.0


class EnvFileError(RuntimeError):
    """`.env` 读写不了时给**人看的一句话**（不带回溯）。"""


def decode_env(raw: bytes) -> tuple[str, str]:
    """按 BOM 判编码，返回（文本, 编码名）。

    编码名是**能原样写回**的那个：`utf-8-sig` 会在写出时把 BOM 加回去，
    `utf-16` 同理——"我改了两个字，git 却显示整个文件都动了"就是这么来的。
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


def split_export_prefix(line: str) -> tuple[str, str]:
    """把 `export KEY=VALUE` 拆成（前缀, 余下部分）。不认时前缀是空串。

    写回时要**留住原文的前缀**（`EXPORT ` 就写回 `EXPORT `）：否则 `source` 这份文件
    的人会发现变量没被导出。
    """
    match = _EXPORT_RE.match(line)
    if match is None:
        return "", line
    return match.group(1), match.group(2).strip()


def iter_env_items(text: str):
    """逐行给出（键, 值）。认 `export ` 前缀，跳过注释与空行。"""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        _, line = split_export_prefix(line)
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        yield key.strip(), value.strip().strip("'\"")


def load_env_file(path: Path) -> dict[str, str]:
    """读最简单的 `KEY=VALUE` 形式。不引入第三方依赖，读坏了也不抛。

    读不出来（不存在 / 是目录 / 没权限 / 编码不对）一律返回空字典：密钥是增强，
    该说"没找到"，而不是让命令崩在启动路径上。**要改这份文件就别用它**——
    用 `update_env_file`，那里会把编码与换行一起还原。
    """
    try:
        text, _, _ = read_env_text(path)
    except EnvFileError:
        return {}
    return dict(iter_env_items(text))


# --- 写路径：向导与 `spool env` 走的是下面这一条 ---


def _temp_candidates(path: Path) -> list[Path]:
    """这个 `.env` 可能留下的临时文件（`mkstemp` 的随机段不固定）。"""
    with contextlib.suppress(OSError):
        return list(path.parent.glob(f"{path.name}.*{_TEMP_SUFFIX}"))
    return []


def _sweep_stale_temps(path: Path) -> None:
    """扫掉陈旧的临时文件。

    为什么需要它：Ctrl-C 之外的死法（被 SIGKILL、断电）**没有任何代码能清理**，
    而临时文件里是明文密钥。所以下一次写入时顺手看一眼——这是唯一不需要守护进程
    也能兜住的时机。
    """
    now = time.time()
    for stale in _temp_candidates(path):
        try:
            if now - stale.stat().st_mtime < _STALE_TEMP_SECONDS:
                continue
            stale.unlink()
        except OSError:
            continue


def _atomic_write(path: Path, payload: bytes) -> None:
    """先写同目录的临时文件，再 `os.replace` 换上去。

    为什么不能直接 `write_bytes`（二轮评审逮出来的）：它是"先截断再写"——实测
    `open(path, "wb")` 一调用，文件就已经是 0 字节了。密钥文件若在写入中途失败
    （磁盘满、进程被杀、断电）就**不可恢复**，而这份文件常常是一个账号唯一的
    一份凭据。临时文件 + 原子替换把结果收敛成"要么旧的、要么新的"。

    两处细节都是评审计出来的：

    - **顺着符号链接写**：`os.replace` 换的是路径本身，而软链的意义是"写到它指向的
      地方"。不 `realpath` 的话，软链 `.env` 会得到"`rc=0` 报已写入、`list` 也显示新值，
      而真实文件仍是旧密钥"——静默的"改了不生效"。
    - **失败时连 Ctrl-C 一起兜**：`except OSError` 只罩得住常规失败，`KeyboardInterrupt`
      会从缝里溜走，把一份含明文密钥的临时文件留在人家目录里。
    """
    real = Path(os.path.realpath(path))
    try:
        real.parent.mkdir(parents=True, exist_ok=True)
        _sweep_stale_temps(real)
        handle_fd, tmp_name = tempfile.mkstemp(
            prefix=f"{real.name}.", suffix=_TEMP_SUFFIX, dir=str(real.parent)
        )
    except OSError as exc:
        raise EnvFileError(f"写不了 {path}：{exc}") from exc
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle_fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())  # 断电之后也认得这份内容
        os.replace(tmp, real)
    except BaseException as exc:  # noqa: BLE001 - 含 KeyboardInterrupt，见上面那条注释
        with contextlib.suppress(OSError):
            tmp.unlink()
        if isinstance(exc, OSError):
            raise EnvFileError(f"写不了 {path}：{exc}") from exc
        raise


def _restrict(path: Path) -> None:
    """POSIX 上收紧成 600（`mkstemp` 本来就是 600，这里是兜底）。

    Windows 上**故意什么都不做**：那边改权限要另一套 API（ACL），而且多半改不对；
    假装保护过了比不保护更坏——它会让人以为这份文件已经安全了。
    """
    if sys.platform == "win32":
        return
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """只替换同名键，其它行（注释、空行、别人的键）原样保留。

    **编码与换行按原文还原**（见模块开头）。写出走 `_atomic_write`：
    `write_text` 除了会按平台把 `\\n` 翻成 `\\r\\n`（Windows 上），也保不住
    "中途失败别把原文件毁掉"这件事。

    目标不存在就建一个（UTF-8、LF）；`export KEY=VALUE` 的前缀照原样留住。
    """
    if path.is_dir():
        raise EnvFileError(f"{path} 是个目录，不是 .env 文件")
    if path.is_file():
        text, encoding, newline = read_env_text(path)
    else:
        text, encoding, newline = "", "utf-8", "\n"
    # 原文件以换行结尾（或是刚建的空文件）就保持这个习惯：有些工具按行读，
    # 少了最后那个换行会把最后一行当成"没写完"。
    trailing = text.endswith(("\n", "\r")) or not text
    written: set[str] = set()
    out: list[str] = []
    for raw in text.splitlines():
        prefix, probe = split_export_prefix(raw.strip())
        if probe and not probe.startswith("#") and "=" in probe:
            key = probe.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{prefix}{key}={updates[key]}")
                written.add(key)
                continue
        out.append(raw)
    for key, value in updates.items():
        if key not in written:
            out.append(f"{key}={value}")
    body = newline.join(out)
    _atomic_write(path, (body + (newline if trailing else "")).encode(encoding))
    _restrict(path)
