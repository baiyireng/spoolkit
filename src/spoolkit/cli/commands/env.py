"""`spool env`：用户级 `.env` 在哪、有哪些键、怎么把凭据搬上去。

为什么要有这条命令：密钥那一层属于"这台机器上的你"，位置是
`%APPDATA%\\spoolkit\\.env`（与 `config.toml` 同目录）——而人不会记得住那个路径，
更不会记得**哪些名字是被认的**。没有它，起一条机器级通道的代价就是每次重打
`--appid/--secret`，或者把密钥写进某个随手建的文件里，然后没人知道它到底在哪。

三条规矩：

1. **不回显值**：`list` 只印键名与位置，写入后只印遮罩过的形状（照
   `bridge.credentials.mask`）。密钥进过一次终端，就等于进过一次滚动缓冲、
   一次截图、一次粘贴。
2. **不动别人的行**：写入只替换同名键，注释、空行、分节、别的键原样保留。
   整份重写的代价是下一个人把手写注释挪走，然后两份配置开始漂移。
3. **位置可覆盖**：`SPOOLKIT_ENV_FILE` 指到哪就写哪（测试与多套配置靠它）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from spoolkit import settings
from spoolkit.bridge.credentials import mask
from spoolkit.envfile import load_env_file

USAGE = (
    "用法：\n"
    "  spool env                     看用户级 .env 的位置与键名（不印值）\n"
    "  spool env path                只印路径（方便脚本里用）\n"
    "  spool env set QQ_AppID=102… QQ_AppSecret=…   写进去（同键替换）\n"
    "  spool env import --from <工作区的 .env> [--only KEY]   从工作区搬过来"
)


def _write_updates(path: Path, updates: dict[str, str]) -> None:
    """只替换同名键，其它行（注释、空行、别人的键）原样保留。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    written: set[str] = set()
    out: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                written.add(key)
                continue
        out.append(raw)
    for key, value in updates.items():
        if key not in written:
            out.append(f"{key}={value}")
    text = "\n".join(out).strip("\n")
    path.write_text((text + "\n") if text else "", encoding="utf-8")
    _restrict(path)


def _restrict(path: Path) -> None:
    """POSIX 上收紧成 600。

    Windows 上**故意什么都不做**：那边改权限要另一套 API（ACL），而且多半改不对；
    假装保护过了比不保护更坏——它会让人以为这份文件已经安全了。
    """
    if sys.platform == "win32":
        return
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _parse_pairs(items: list[str]) -> tuple[dict[str, str], list[str]]:
    """把 `KEY=VALUE` 解析成字典；不是这个形状的原样退回去（好让报错点名）。"""
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
    if not path.is_file():
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
        print("这些参数不是 KEY=VALUE 的形状：" + "、".join(bad), file=sys.stderr)
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
    source = Path(source_raw).expanduser()
    if not source.is_file():
        print(f"来源不是一个文件：{source}", file=sys.stderr)
        return 1
    table = load_env_file(source)
    if not table:
        print(
            f"{source} 里一行都没解析出来（只认 KEY=VALUE 的文本；可能是编码问题）",
            file=sys.stderr,
        )
        return 1
    picked = {key: value for key, value in table.items() if not only or key in only}
    missing = [key for key in only if key not in table]
    if not picked:
        print(f"{source} 里没有这些键：{'、'.join(only)}", file=sys.stderr)
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
    print(USAGE, file=sys.stderr)
    return 2
