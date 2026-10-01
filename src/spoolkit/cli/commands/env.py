"""`spool env`：用户级 `.env` 在哪、有哪些键、怎么把凭据搬上去。

为什么要有这条命令：密钥那一层属于"这台机器上的你"，位置是
`%APPDATA%\\spoolkit\\.env`（与 `config.toml` 同目录）——而人不会记得住那个路径，
更不会记得**哪些名字是被认的**。没有它，起一条机器级通道的代价就是每次重打
`--appid/--secret`，或者把密钥写进某个随手建的文件里，然后没人知道它到底在哪。

五条规矩（前四条各有用例钉着，第五条是三轮评审逼出来的）：

1. **不回显值**：`list` 只印键名与位置，写入后只印遮罩过的形状（照
   `bridge.credentials.mask`）。密钥进过一次终端，就等于进过一次滚动缓冲、
   一次截图、一次粘贴——**连报错也不例外**。注意这条的边界：**用户自己敲进来的
   参数一律不印**（`set` 的不合形状项、`--only` 的任一个值——它们可能本来就是密钥），
   要印就印**从文件里读出来的键名**（那是文件自己的内容，`list` 早就印了）。
2. **不动别人的行**：写入只替换同名键，注释、空行、分节、别的键原样保留。
   整份重写的代价是下一个人把手写注释挪走，然后两份配置开始漂移。
3. **读写同一套判断**：编码（UTF-8 / UTF-8 BOM / UTF-16）与换行（CRLF / LF）
   都按原文还原，符号链接**顺着写**（换掉链接本身＝"报成功、实际没生效"）。
   只读时读得动、一 `set` 就崩或无提示地追加一份重复键，是最难查的一类故障：
   看着能用，一改就坏。
4. **位置可覆盖**：`SPOOLKIT_ENV_FILE` 指到哪就写哪（测试与多套配置靠它）。
5. **写入必须是原子的、也不许留垃圾**：先写同目录临时文件再 `os.replace`；失败
   （含 Ctrl-C）删掉临时文件；下一次写入一并清掉**陈旧的**临时文件：临时文件里是
   **明文密钥**，不能留在用户目录里。另外：目标被别的进程占着（Windows 上
   常见）时替换会失败，这时报错走人、原文件不动，关掉占用再重试即可。

第 3、5 条**不在这个文件里实现**：读与写都在 `spoolkit/envfile.py`
（`read_env_text` 读原文的三个要素、`update_env_file` 原样写回），因为**向导
（`onboarding`）写的是同一个文件、要的是同一套判断**——那是 Task 2 评审逼出来的
教训，两份实现迟早漂移，而漂移的代价是"看着能用、一改就炸"。这里只负责把
参数（键值对、位置）交给它。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from spoolkit import settings
from spoolkit.bridge.credentials import mask
from spoolkit.envfile import (
    EnvFileError,
    load_env_file,
    update_env_file,
)

USAGE = (
    "用法：\n"
    "  spool env                     查看用户级 .env 的位置与键名（不印值）\n"
    "  spool env path                只印路径（供脚本使用）\n"
    "  spool env set QQ_AppID=102… QQ_AppSecret=…   写入（同键替换）\n"
    "  spool env import --from <工作区的 .env> [--only KEY]   从工作区导入"
)

# 临时文件的命名、陈旧阈值与那五个写辅助（`_temp_candidates` / `_sweep_stale_temps`
# / `_atomic_write` / `_restrict` / `_write_updates`）都搬到了 `spoolkit/envfile.py`：
# 向导写的是同一份文件，读写的判断必须只有一处（见模块开头）。这里不再留副本。


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
        print("  该路径是个目录，不是文件（`SPOOLKIT_ENV_FILE` 可能指向有误）。")
    elif not path.is_file():
        print(
            "  不存在。两种建立方式：\n"
            "    spool env set KEY=VALUE              直接写入\n"
            "    spool env import --from <工作区的 .env>   从工作区已有的一份导入"
        )
    else:
        table = load_env_file(path)
        if table:
            print(f"  内有 {len(table)} 个键（只印键名与形状，不印值）：")
            for key in sorted(table):
                print(f"    {key} = {mask(table[key])}")
        else:
            print("  存在，但未解析出任何一行（只识别 KEY=VALUE 文本；可能是编码问题）")
    print(f"用户级 config.toml：{settings.effective_config_path()}")
    print(
        "工作区级那一层随工作区变化（`<工作区>/.env`）；机器级服务（通道 / 网页壳 / MCP）"
        "读的是用户级这一层，所以 QQ 之类的凭据放这里即可，无需关心在哪个目录启动。"
    )
    return 0


def _do_set(items: list[str]) -> int:
    updates, bad = _parse_pairs(items)
    if bad:
        # **点名个数，不点名内容**：打错的常常就是密钥本身（少了个 `=`），
        # 原样回显等于把它打进终端。
        print(
            f"有 {len(bad)} 个参数不是 KEY=VALUE 的形状（不回显，以免把值写入终端）。",
            file=sys.stderr,
        )
        print(USAGE, file=sys.stderr)
        return 2
    if not updates:
        print("未给出 KEY=VALUE。", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2
    path = settings.env_path()
    update_env_file(path, updates)
    print(f"已写入 {path}（只印形状，不印值）：")
    for key, value in updates.items():
        print(f"  {key} = {mask(value)}")
    print("避免密钥进入 shell 历史：用 `spool env import --from <工作区的 .env>`。")
    return 0


def _do_import(source_raw: str, only: list[str]) -> int:
    source_raw = str(source_raw or "").strip()
    if not source_raw:
        print("需要指定来源。", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2
    wanted = [str(item).strip() for item in only]
    if any(not item for item in wanted):
        # `--only ""` 会被当成"给了一个名字叫空串的键"，然后输出说"没搬"，
        # 那句话说明不了问题出在哪。
        print("`--only` 的值不能为空。", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2
    source = Path(source_raw).expanduser()
    if not source.is_file():
        # 路径要印出来（不然人不知道它找的是哪儿）；这里印的是**参数本身**，
        # 不是内容——与"不回显值"那条不冲突。
        print(f"来源路径不是文件：{source}", file=sys.stderr)
        return 1
    table = load_env_file(source)
    if not table:
        print(
            f"{source} 未解析出任何一行（只识别 KEY=VALUE 文本；可能是编码问题）",
            file=sys.stderr,
        )
        return 1
    picked = {key: value for key, value in table.items() if not wanted or key in wanted}
    missing = [key for key in wanted if key not in table]
    if not picked:
        # **`--only` 的值一律不印**：它可能就是一个密钥（人少打了键名），而
        # "像不像键名"根本判不出来——`sk_live_…`、`AKIA…`、`github_pat_…` 全长得像。
        # 要印就印**文件里真有哪些键**（那是文件自己的内容，`list` 早就印了）。
        print(
            f"{len(wanted)} 个 `--only` 在来源里一个都没找到（不回显所给的值）。",
            file=sys.stderr,
        )
        print(f"{source} 里有这些键：{'、'.join(sorted(table))}", file=sys.stderr)
        return 1
    target = settings.env_path()
    update_env_file(target, picked)
    print(f"从 {source} 导入 {target}（{len(picked)} 个键；来源文件未修改）：")
    for key in sorted(picked):
        print(f"  {key} = {mask(picked[key])}")
    if missing:
        print(f"另有 {len(missing)} 个 `--only` 来源里没有，未导入（不回显所给的值）。")
        print(f"{source} 里可用的键：{'、'.join(sorted(table))}")
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
