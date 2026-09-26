"""`spool env`：用户级 `.env` 在哪、有哪些键、怎么把凭据搬上去。

为什么要有这条命令：密钥那一层属于"这台机器上的你"，位置是
`%APPDATA%\\spoolkit\\.env`（与 `config.toml` 同目录）——而人不会记得住那个路径，
更不会记得**哪些名字是被认的**。没有它，起一条机器级通道的代价就是每次重打
`--appid/--secret`，或者把密钥写进某个随手建的文件里，然后没人知道它到底在哪。

五条规矩（前四条各有用例钉着，第五条是两轮评审逼出来的）：

1. **不回显值**：`list` 只印键名与位置，写入后只印遮罩过的形状（照
   `bridge.credentials.mask`）。密钥进过一次终端，就等于进过一次滚动缓冲、
   一次截图、一次粘贴——**连报错也不例外**（不合形状的参数、不像键名的 `--only`
   都只报个数）。
2. **不动别人的行**：写入只替换同名键，注释、空行、分节、别的键原样保留。
   整份重写的代价是下一个人把手写注释挪走，然后两份配置开始漂移。
3. **读写同一套判断**：编码（UTF-8 / UTF-8 BOM / UTF-16）与换行（CRLF / LF）
   都按原文还原。只读时读得动、一 `set` 就崩或悄悄追加一份重复键，是最难查的
   "看着能用、一改就炸"。
4. **位置可覆盖**：`SPOOLKIT_ENV_FILE` 指到哪就写哪（测试与多套配置靠它）。
5. **写入必须是原子的**：先写同目录临时文件再 `os.replace`。密钥文件被截断到一半
   就**不可恢复**了——这份文件往往是一个账号唯一的一份凭据。
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import sys
import tempfile
from pathlib import Path

from spoolkit import settings
from spoolkit.bridge.credentials import mask
from spoolkit.envfile import (
    EnvFileError,
    load_env_file,
    read_env_text,
    split_export_prefix,
)

USAGE = (
    "用法：\n"
    "  spool env                     看用户级 .env 的位置与键名（不印值）\n"
    "  spool env path                只印路径（方便脚本里用）\n"
    "  spool env set QQ_AppID=102… QQ_AppSecret=…   写进去（同键替换）\n"
    "  spool env import --from <工作区的 .env> [--only KEY]   从工作区搬过来"
)

# 键名的形状。`--only` 只接受这种；别的一律拒，且**不回显**（见 `_do_import`）。
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _atomic_write(path: Path, payload: bytes) -> None:
    """先写同目录的临时文件，再 `os.replace` 换上去。

    为什么不能直接 `write_bytes`（二轮评审逮出来的）：它是"先截断再写"——实测
    `open(path, "wb")` 一调用，文件就已经是 0 字节了。密钥文件若在写入中途失败
    （磁盘满、进程被杀、断电）就**不可恢复**，而这份文件常常是一个账号唯一的
    一份凭据。临时文件 + 原子替换把结果收敛成"要么旧的、要么新的"。

    临时文件建在**同目录**：跨盘改名不是原子的（`os.replace` 会退化成拷贝）。
    `mkstemp` 在 POSIX 上就是 0600，替换过去之后权限也不会松。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle_fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
    except OSError as exc:
        raise EnvFileError(f"写不了 {path}：{exc}") from exc
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle_fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())  # 断电之后也认得这份内容
        os.replace(tmp, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()  # 失败就别在人家目录里留半个临时文件
        raise EnvFileError(f"写不了 {path}：{exc}") from exc


def _write_updates(path: Path, updates: dict[str, str]) -> None:
    """只替换同名键，其它行（注释、空行、别人的键）原样保留。

    **编码与换行按原文还原**（见模块开头第 3 条）。写出走 `_atomic_write`：
    `write_text` 除了会按平台把 `\\n` 翻成 `\\r\\n`（Windows 上），也保不住
    "中途失败别把原文件毁掉"这件事。
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


def _restrict(path: Path) -> None:
    """POSIX 上收紧成 600（`mkstemp` 本来就是 600，这里是兜底）。

    Windows 上**故意什么都不做**：那边改权限要另一套 API（ACL），而且多半改不对；
    假装保护过了比不保护更坏——它会让人以为这份文件已经安全了。
    """
    if sys.platform == "win32":
        return
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def _parse_pairs(items: list[str]) -> tuple[dict[str, str], list[str]]:
    """把 `KEY=VALUE` 解析成字典；不是这个形状的**只报个数**（见模块开头第 1 条）。"""
    updates: dict[str, str] = {}
    bad: list[str] = []
    for item in items:
        if "=" not in item:
            bad.append(item)
            continue
        key, _, value = item.partition("=")
        key = key.strip()
        if not key:
            bad.append(item)
            continue
        updates[key] = value.strip()
    return updates, bad


def _print_list() -> int:
    path = settings.env_path()
    print(f"用户级 .env：{path}")
    if path.is_dir():
        print("  —— 这个路径是个目录，不是文件（`SPOOLKIT_ENV_FILE` 指错了？）")
    elif not path.is_file():
        print(
            "  不存在。建它的两种办法：\n"
            "    spool env set KEY=VALUE              直接写\n"
            "    spool env import --from <工作区的 .env>   把某个工作区里已经有的搬上来"
        )
    else:
        table = load_env_file(path)
        if table:
            print(f"  里面有 {len(table)} 个键（只印名字与形状，不印值）：")
            for key in sorted(table):
                print(f"    {key} = {mask(table[key])}")
        else:
            print("  存在，但一行都没解析出来（只认 KEY=VALUE 的文本；可能是编码问题）")
    print(f"用户级 config.toml：{settings.effective_config_path()}")
    print(
        "工作区级那一层跟着工作区走（`<工作区>/.env`）；**机器级服务（通道 / 网页壳 / MCP）"
        "读的是这一层**，所以 QQ 之类的凭据放这里，就不必在哪个目录起命令。"
    )
    return 0


def _do_set(items: list[str]) -> int:
    updates, bad = _parse_pairs(items)
    if bad:
        # **点名个数，不点名内容**：打错的常常就是密钥本身（少了个 `=`），
        # 原样回显等于把它打进终端。
        print(
            f"有 {len(bad)} 个参数不是 KEY=VALUE 的形状（不回显，免得把值打进终端）。",
            file=sys.stderr,
        )
        print(USAGE, file=sys.stderr)
        return 2
    if not updates:
        print("没给 KEY=VALUE。", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2
    path = settings.env_path()
    _write_updates(path, updates)
    print(f"已写入 {path}（只印形状，不印值）：")
    for key, value in updates.items():
        print(f"  {key} = {mask(value)}")
    print("（想避免密钥进 shell 历史，用 `spool env import --from <工作区的 .env>`）")
    return 0


def _do_import(source_raw: str, only: list[str]) -> int:
    source_raw = str(source_raw or "").strip()
    if not source_raw:
        print("要指定来源。", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2
    wanted = [str(item).strip() for item in only]
    if any(not item for item in wanted):
        # `--only ""` 会被当成"给了一个名字叫空串的键"，然后输出说"没搬"
        # ——那不是在说人话。
        print("`--only` 后面不能是空的。", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2
    if any(not _KEY_RE.match(item) for item in wanted):
        # **不回显**：`--only` 这个位置上打错的常常是密钥本身（少打了键名），
        # 原样回显等于把它打进终端——与 `set` 那条同一个道理。
        print(
            f"有 {len(wanted)} 个 `--only` 里出现了不像键名的值（只认字母/数字/下划线，"
            "且不以数字开头）。不回显内容，免得把值打进终端。",
            file=sys.stderr,
        )
        print(USAGE, file=sys.stderr)
        return 2
    source = Path(source_raw).expanduser()
    if not source.is_file():
        # 路径要印出来（不然人不知道它找的是哪儿）；这里印的是**参数本身**，
        # 不是内容——与"不回显值"那条不冲突。
        print(f"来源不是一个文件：{source}", file=sys.stderr)
        return 1
    table = load_env_file(source)
    if not table:
        print(
            f"{source} 里一行都没解析出来（只认 KEY=VALUE 的文本；可能是编码问题）",
            file=sys.stderr,
        )
        return 1
    picked = {key: value for key, value in table.items() if not wanted or key in wanted}
    missing = [key for key in wanted if key not in table]
    if not picked:
        print(f"{source} 里没有这些键：{'、'.join(wanted)}", file=sys.stderr)
        print(f"它有的是：{'、'.join(sorted(table))}", file=sys.stderr)
        return 1
    target = settings.env_path()
    _write_updates(target, picked)
    print(f"从 {source} 搬到 {target}（{len(picked)} 个键；**来源文件没动**）：")
    for key in sorted(picked):
        print(f"  {key} = {mask(picked[key])}")
    if missing:
        print("这些键来源里没有，没搬：" + "、".join(missing))
    return 0


def env_command(args: argparse.Namespace) -> int:
    action = str(getattr(args, "action", "list") or "list")
    try:
        if action == "path":
            print(settings.env_path())
            return 0
        if action == "set":
            return _do_set([str(item) for item in (getattr(args, "items", []) or [])])
        if action == "import":
            return _do_import(
                str(getattr(args, "source", "") or ""),
                [str(item) for item in (getattr(args, "only", []) or [])],
            )
        if action == "list":
            return _print_list()
    except EnvFileError as exc:
        # 读写不了就说一句人话，别把回溯甩给人（这类失败都是"路径/权限/编码不对"，
        # 回溯里那几十行对判断下一步毫无帮助）。**读路径与写路径都要罩住**——
        # 二轮评审发现的另一条 Critical 就是"只读路径罩了，写路径漏了"。
        print(str(exc), file=sys.stderr)
        return 1
    print(USAGE, file=sys.stderr)
    return 2
