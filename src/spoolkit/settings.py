"""用户级默认配置。

为什么需要它：配置有三层，缺一层就会别扭——

- **工作区级**（`.agent/limits.json`、`.agent/policy.json`）：属于这个项目，
  跟着项目走，已经齐了；
- **命令行**：一次一给，用于临时覆盖；
- **用户级**（本模块 + 同目录的 `.env`）：属于这台机器上的你。缺了它，同一件事
  每天都要重打一遍 `--provider llamacpp --base-url http://127.0.0.1:8080`——
  而这类重复输入最后的下场是被人做成一个 .bat，然后没人知道真正的配置在哪。
  **密钥只放 `.env`**（`%APPDATA%\\spoolkit\\.env`，见 `env_path()`），本文件只放
  非密钥默认值：TOML 有键白名单，写错一个字母的症状是"设置了却没生效"，
  而密钥得能随手加一行。

优先级：**命令行 > 环境变量 > 用户配置 > 内置默认**。顺序不能反：命令行是
本次运行的意图，环境变量是这次 shell 的意图，配置文件是长期默认。

文件位置：`SPOOLKIT_CONFIG` 指到哪就用哪；否则 Windows 用
`%APPDATA%\\spoolkit\\config.toml`，其它平台用 `~/.config/spoolkit/config.toml`。
**不放在工作区里**：工作区是要提交、要分享的东西，不该承载某台机器的默认值。

本机外接也写在这份文件的 `[[service]]` 里（不进仓库）。常见两段、名字可改：

- **llama**：本机语言模型进程。hub / serve 按 `provider` / `base_url` 探活；
  配方只方便 `spool service start/stop`，已经通了不重复拉起。
- **tunnel**：给聊天通道出网的本机 SOCKS（通常是 `ssh -D`）。QQ 等平台有
  IP 白名单时，借跳板把出口固定成那台主机；`spool hub --bridge-proxy
  socks5://127.0.0.1:1080` 去连它。这和 `--proxy`（模型供应商）不是一回事。

hub **只探活、不启**这些进程。没写这张表时，隧道不探、不拉。

通道的默认值写在 `[channel.<名字>]` 子表里（`proxy` / `policy` / `scope` /
`timeout` / `web` / `root`），给 `spool qq` 这类短命令当默认值用：

```toml
[channel.qqbot]
proxy   = "socks5://127.0.0.1:1080"   # 等价于 --bridge-proxy
policy  = "ask"
scope   = "**"
timeout = 240                          # 秒，与 spool bridge --timeout 同一个单位
web     = true
```

它是**子表而不是扁平键**：通道是按名字来的，插件还能注册新通道，扁平键会把
"有哪些通道"写死在白名单里（`[[mcp]]` / `[[service]]` 是同一条理由）。优先级
仍是**命令行 > 环境变量 > 用户配置 > 内置默认**，环境变量名是
`SPOOLKIT_CHANNEL_<通道名>_<键>`。认不出的键名**要说出口**，取值写错的
（`policy = "AUTO"`、`timeout = -5`）**同样要说出口**——写错一个字母的症状是
"设置了却没生效"，而那种问题只靠复现查不出来。

改名（`agents-dev` → `spoolkit`）时留了一层兼容：**新位置没有文件而旧位置有，
就继续读旧的那份**。所以已经配好的人一个字都不用改；等他下次 `config --set`，
配置写到新位置，旧的那份留在原地当备份（不再读）。
"""

import os
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from spoolkit.envfile import load_env_file
from spoolkit.policy import POLICIES

# 能配置的键。写成一个白名单而不是"想存什么存什么"：
# 配置文件的键写错一个字母，症状是"设置了但没生效"，而那种问题最难查。
KEYS: tuple[str, ...] = (
    "provider",
    "model",
    "base_url",
    "proxy",
    "script",
    "notify_disconnect",
    "web_search_api",
)

# 对应的环境变量名：SPOOLKIT_<KEY 大写>。
ENV_PREFIX = "SPOOLKIT_"
# 改名前的旧前缀。**故意继续认**：别人 .bat / 快捷方式里写好的
# `AGENTS_DEV_PROVIDER` 不该因为我们改了个名字就失效。新前缀优先。
LEGACY_ENV_PREFIX = "AGENTS_DEV_"


def config_path() -> Path:
    """配置文件位置。`SPOOLKIT_CONFIG` 优先（测试与多套配置用）。"""
    override = os.environ.get("SPOOLKIT_CONFIG") or os.environ.get("AGENTS_DEV_CONFIG")
    if override:
        return Path(override)
    return _default_dir() / "config.toml"


def legacy_config_path() -> Path:
    """改名前的配置位置。只在"新的那份还不存在"时被读。"""
    if sys.platform == "win32":
        root = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "agents-dev" / "config.toml"


def _default_dir() -> Path:
    """这台机器上放用户级配置的目录。"""
    if sys.platform == "win32":
        root = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "spoolkit"


# 用户级 `.env` 的位置覆盖。测试与"多套配置"靠它隔离——**不许写死 `%APPDATA%`**。
ENV_FILE_VAR = "SPOOLKIT_ENV_FILE"
LEGACY_ENV_FILE_VAR = "AGENTS_DEV_ENV_FILE"


def env_path() -> Path:
    """用户级 `.env` 的位置：**密钥放这里，非密钥默认值放 `config.toml`**。

    为什么和 `config.toml` 同目录：它们是同一层（"这台机器上的你"），分两处只会
    让人记不住哪个在哪。为什么密钥不进 TOML：`config.toml` 有键白名单，写错一个
    字母的症状是"设置了却没生效"；而密钥得能随手加一行、能被 `.gitignore`
    一句话排除。
    """
    override = os.environ.get(ENV_FILE_VAR) or os.environ.get(LEGACY_ENV_FILE_VAR)
    if override:
        return Path(override)
    return _default_dir() / ".env"


def secret_paths(workspace: Path | None = None) -> list[tuple[str, Path]]:
    """解析密钥时按顺序看的位置：**工作区级在前，用户级在后**。

    "更具体的赢"——项目里写的那份盖住个人默认那份。`workspace=None`（机器级
    服务：桥 / 网页壳 / MCP）就只剩用户级那一份。
    """
    paths: list[tuple[str, Path]] = []
    if workspace is not None:
        paths.append(("工作区", Path(workspace) / ".env"))
    paths.append(("用户级", env_path()))
    return paths


def resolve_secret(
    names: Sequence[str],
    workspace: Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[str, str]:
    """返回（值, 来源说明）。顺序：**环境变量 → 工作区 `.env` → 用户级 `.env`**。

    `workspace` 只表示"这次运行绑定的工作区"，**不是当前目录**：机器级入口服务
    整台机器，"你碰巧站在哪个目录"不该改变它读哪份密钥（2026-09-27 的真实事故：
    在 `C:\\Users\\32615` 起桥，于是它去找 `C:\\Users\\32615\\.env`，而凭据在别处）。

    `names` 是别名列表，**先给的先赢**（`SPOOLKIT_QQ_APPID` 排在 `QQ_AppID` 前面）。

    每次调用都重读文件，不缓存：桥与网页壳是长驻进程，凭据是会被改的。
    """
    environment = os.environ if env is None else env
    for name in names:
        value = (environment.get(name) or "").strip()
        if value:
            return value, f"环境变量 {name}"
    for label, path in secret_paths(workspace):
        table = load_env_file(path)
        for name in names:
            value = (table.get(name) or "").strip()
            if value:
                return value, f"{label} {path} 里的 {name}"
    return "", "没找到（环境变量、工作区 .env、用户级 .env 都没有）"


def describe_secret_search(names: Sequence[str], workspace: Path | None = None) -> str:
    """把"找过哪些位置、结果如何"写出来——**只印键名，绝不印值**。

    为什么值得单独写一段：密钥类问题里最常见的一句是"我明明填了"，而只靠复现是
    猜不出它到底看了哪儿的（`describe_missing` 就是这么来的）。

    四种情况要分开说，因为它们指向完全不同的下一步：**不存在**（去建）、
    **不是文件**（路径指到目录了）、**存在但一行都没解析出来**（多半是编码，
    例如记事本存的 UTF-16 或非文本文件）、**没有要找的键**（那就把文件里
    有哪些键列出来，通常一眼就能看出拼写差在哪）。
    """
    lines = ["找过这些位置："]
    set_names = [name for name in names if (os.environ.get(name) or "").strip()]
    if set_names:
        lines.append(f"  环境变量 —— 已设置：{'、'.join(set_names)}")
    else:
        lines.append(f"  环境变量 —— 都没设置（认这些名字：{'、'.join(names)}）")
    for label, path in secret_paths(workspace):
        if not path.exists():
            lines.append(f"  [{label}] {path} —— 不存在")
            continue
        if not path.is_file():
            lines.append(f"  [{label}] {path} —— 不是文件（路径指到目录上了？）")
            continue
        table = load_env_file(path)
        if not table:
            lines.append(
                f"  [{label}] {path} —— 存在（{_size_hint(path)}），"
                "但一行都没解析出来（编码？只认 KEY=VALUE 的文本）"
            )
            continue
        present = [name for name in names if (table.get(name) or "").strip()]
        blank = [
            name for name in names if name in table and not (table.get(name) or "").strip()
        ]
        if present:
            lines.append(f"  [{label}] {path} —— 里面有：{'、'.join(present)}")
        elif blank:
            # `QQ_AppID=`（键写了、值空着）是最容易骗过自己的写法：文件里"明明有这一行"，
            # 但等于没配。先说清这一种，再轮到"根本没这个键"。
            lines.append(
                f"  [{label}] {path} —— 有这些键但值是空的（等于没配）："
                f"{'、'.join(blank)}"
            )
        else:
            lines.append(
                f"  [{label}] {path} —— 没有要找的键（它有：{'、'.join(sorted(table))}）"
            )
    return "\n".join(lines)


def _size_hint(path: Path) -> str:
    """给"存在但解析不出"那句配个大小：0 字节就是空文件，否则是"有东西但读不懂"。"""
    try:
        size = path.stat().st_size
    except OSError:
        return "读不出大小"
    return "空文件，0 字节" if size == 0 else f"{size} 字节"


def effective_config_path() -> Path:
    """**实际会读到**的那一份。改名兼容就发生在这里（见模块开头）。

    显式指定过位置（`SPOOLKIT_CONFIG` / 旧名）时不猜：**指定就是指定**。
    否则"把配置指到一个空文件"会被旧位置那份真实配置悄悄盖住——测试里
    正是这么栽的（开发机上的 provider 泄漏进了断言）。
    """
    target = config_path()
    if os.environ.get("SPOOLKIT_CONFIG") or os.environ.get("AGENTS_DEV_CONFIG"):
        return target
    if target.is_file():
        return target
    legacy = legacy_config_path()
    return legacy if legacy.is_file() else target


def describe_missing() -> str:
    """向导要弹之前，把"找过哪儿、结果如何"说清楚。

    为什么值得单独写一段：改名之后配置有两个可能的位置（新的 `spoolkit`、旧的
    `agents-dev`），"它又问了我一遍"这件事本来就容易让人怀疑代码。真发生过一次
    用户机器上弹出向导，而**当时那份旧配置明明还在**——问题是我们没有留下任何
    "它到底看了哪儿"的痕迹，于是只能靠复现去猜。这一行输出就是那条痕迹。
    """
    lines = ["没找到供应商配置，找过这些位置："]
    for label, path in (("新位置", config_path()), ("改名前的旧位置", legacy_config_path())):
        if not path.is_file():
            lines.append(f"  [{label}] {path} —— 不存在")
            continue
        payload, error = read_config(path)
        if error:
            lines.append(f"  [{label}] {path} —— 存在，但读不出来：{error}")
        else:
            keys = "、".join(sorted(payload)) or "空文件"
            lines.append(f"  [{label}] {path} —— 存在，里面有：{keys}")
    override = os.environ.get("SPOOLKIT_CONFIG") or os.environ.get("AGENTS_DEV_CONFIG")
    if override:
        lines.append(f"  注意：位置被环境变量指到了 {override}")
    return "\n".join(lines)


def load(path: Path | None = None) -> dict[str, str]:
    """读配置。文件不存在或读坏了都返回空——配置是增强，不该挡住启动。"""
    payload, _ = read_config(path)
    return {
        key: str(payload[key])
        for key in KEYS
        if isinstance(payload.get(key), (str, int, float))
    }


def read_config(path: Path | None = None) -> tuple[dict, str]:
    """读整个配置文件，返回（内容, 错误说明）。

    **错误要能说出口**。原先解析失败一律返回空，症状是"我明明配了却没生效"
    ——而人只会怀疑代码。TOML 里最常踩的坑正是 Windows 路径：
    `command = "D:\\tools\\x.exe"` 少一个反斜杠就整份读不出来。
    """
    target = path or effective_config_path()
    if not target.is_file():
        return {}, ""
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        return {}, f"读不了 {target}：{exc}"
    try:
        return tomllib.loads(text), ""
    except tomllib.TOMLDecodeError as exc:
        return {}, (
            f"{target} 解析失败：{exc}\n"
            "提示：Windows 路径里的反斜杠要写成双反斜杠（D:\\\\tools\\\\x.exe）"
            "或者用单引号字符串（'D:\\tools\\x.exe'）。"
        )


def _quote(value: str) -> str:
    """写进 TOML 的字符串要转义——反斜杠不转义就会把整份配置写坏。"""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def save(values: dict[str, str], path: Path | None = None) -> Path:
    """写白名单里的键，并保留 `[channel.*]` / `[[mcp]]` / `[[service]]` 以及
    白名单以外的顶层标量（例如人手写的 `tavily_api_key`）。

    数组表与子表都不在 KEYS 里；以前整文件重写会把它们抹掉——
    `spool config --set` 一次，外挂工具、本机配方、通道默认值和搜索 key 就丢了。
    通道那一节尤其亏：它是人手写进去的（`--set` 管不到子表），丢了之后
    `spool qq` 只是"参数回到了默认值"，不会报任何错。
    """
    target = path or config_path()
    existing, error = read_config(target)
    if error:
        existing = {}
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# spoolkit 的用户级默认配置。命令行与环境变量优先于这里。",
        "# 看当前生效值：spool config",
        "",
    ]
    for key in KEYS:
        if key in values and values[key] != "":
            lines.append(f"{key} = {_quote(values[key])}")
    for key, raw in existing.items():
        if key in KEYS or key in ("mcp", "service", "channel"):
            continue
        if isinstance(raw, (str, int, float, bool)):
            lines.append(f"{key} = {_toml_value(raw)}")
    for table in ("mcp", "service"):
        for item in existing.get(table) or []:
            if not isinstance(item, dict):
                continue
            lines.append("")
            lines.append(f"[[{table}]]")
            lines.extend(_dump_table_item(item))
    # 通道小节放在最后：它们是子表，混在顶层标量中间会让 TOML 的归属变难读
    # （顶层键写进 `[channel.x]` 之后会变成那个子表的键——这是真会改语义的一种）。
    channels = existing.get("channel")
    if isinstance(channels, dict):
        for name in sorted(str(item) for item in channels):
            section = channels.get(name)
            if not isinstance(section, dict):
                continue
            lines.append("")
            lines.append(f"[channel.{name}]")
            lines.extend(
                f"{key} = {_toml_value(value)}"
                for key, value in section.items()
                # None 不是 TOML 值（`tomllib` 也解析不出来），原样写回去会写出一份
                # **读不回来**的配置——那比丢一节更坏。
                if value is not None
            )
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        inner = ", ".join(
            f"{key} = {_toml_value(item)}" for key, item in value.items()
        )
        return "{ " + inner + " }" if inner else "{}"
    return _quote(str(value))


def _dump_table_item(item: dict) -> list[str]:
    order = ("name", "command", "args", "env", "health")
    keys = [key for key in order if key in item]
    keys.extend(key for key in item if key not in keys)
    return [f"{key} = {_toml_value(item[key])}" for key in keys]


# --- 通道的默认值：`[channel.<名字>]` --------------------------------------
#
# 为什么单独开一节而不是往 `KEYS` 里再塞几个键：这些值**不属于某一个工作区**，
# 而是"这台机器上这条通道怎么起"——QQ 出网走哪条 SOCKS、默认什么授权策略、
# 一轮等多久。原先起一条通道要敲六个参数（`--channel qqbot --bridge-proxy … --policy
# ask --scope "**" --timeout 240 --web`），而这六个没有一个属于"这一次"。
#
# 为什么不用扁平键（`channel_qqbot_proxy` 之类）：通道是按名字来的，**可能有插件
# 注册的新通道**，扁平键会把"有哪些通道"写死在白名单里；子表 `[channel.<名字>]`
# 谁都能加一节，核心不认识它也能读出来。

# 每个通道能配的键（值 = 内置默认）。`timeout` 的单位是**秒**，与
# `spool bridge --timeout`（默认 900 秒）同一个口径——单位不一致是"配了却没生效"
# 里最难查的一种。
#
# ⚠️ 这六项**不是都被短命令读**：`shortcuts._CHANNEL_OPTION_KEYS` 只认五个，
# `root` 不在里面（工作区在 `main()` 那一步就解析完了，短命令不许回头拿配置补它）。
# 配在文件里却不生效的键必须**看起来就与别人不一样**，所以 `spool config` 会把它
# 标出来（`commands/config.py` 的 `_NOT_READ_BY_SHORTCUTS`，另有测试钉住两处一致）。
CHANNEL_KEYS: dict[str, object] = {
    "proxy": "",  # 通道自己出网走的代理，等价于 --bridge-proxy
    "policy": "",  # 子进程的授权策略（ask / auto / deny）
    "scope": "",  # 允许自动落盘的范围
    "timeout": 900.0,
    "web": True,
    "root": "",  # 钉住的工作区；留空 = 服务整台机器
}

# 通道键 → 环境变量名的中段。`SPOOLKIT_CHANNEL_QQBOT_PROXY`、旧前缀同形。
CHANNEL_ENV_PREFIX = "CHANNEL_"

# 环境变量里要按数字读的键（配置文件里 `timeout = 240` 是 TOML 整数，
# 而环境变量永远是字符串）。
_CHANNEL_NUMERIC = ("timeout",)
_CHANNEL_BOOL = ("web",)

# 认得的取值**只有这几个**的键：它们是**枚举**，不是自由文本。
# 命令行那条路上 argparse 的 `choices` 已经把拼错的挡在门外，而配置文件
# 原先谁都不看——`policy = "AUTO"` 会静默通过，然后在 `cli/settle.py` 里
# 落到"问"那一条路上。那正是本任务反复要消灭的形状："我明明配了自动，它还在问我"。
# **空串是合法取值**（等于没表态：`resolve_policy` 见到假值就去读已保存的策略）。
# 取值表借 `policy.POLICIES`，不另抄一份——抄一份的下场是两处慢慢走散。
_CHANNEL_ENUM: dict[str, tuple[str, ...]] = {"policy": POLICIES}


def _channel_env_key(name: str, key: str) -> str:
    """`qqbot` + `proxy` → `QQBOT_PROXY`。连字符按环境变量的习惯写成下划线。"""
    return f"{name}_{key}".upper().replace("-", "_")


def load_channels(path: Path | None = None) -> dict[str, dict]:
    """用户配置里的 `[channel.<名字>]` 各小节，**原样**返回（不做类型转换）。

    读不到、读坏了都返回空——通道默认值是增强，不该挡住启动（与 `[[mcp]]` /
    `[[service]]` 同一条契约）。非字典的那些（例如人手写了个
    `channel = "qqbot"`）跳过：它们连"键值对"都不是，说不成是配置。
    """
    payload, _ = read_config(path)
    raw = payload.get("channel")
    if not isinstance(raw, dict):
        return {}
    return {
        str(name): dict(item)
        for name, item in raw.items()
        if isinstance(item, dict)
    }


def _coerce_channel_value(key: str, value):
    """从配置文件/环境变量读来的值 → 该有的类型。认不出返回 `None`。

    `None` 表示"这个值用不了"，由调用方**说出来**（`ChannelValues.issues`）并
    退回下一层——静默跳过正是"设置了却没生效"的来源。

    三条判定，都只做这一层该做的事（"这个值读不读得懂"）：

    - 开关（`web`）：认 `true/false`、`1/0`、`yes/no`、`on/off`（大小写不论）。
      宽容是故意的：环境变量那一层永远是字符串，`SPOOLKIT_CHANNEL_QQBOT_WEB=no`
      是自然的写法，而且这两种写法**都不会改变"配了什么"**——`yes` 就是"开"；
    - 数字（`timeout`）：要能当秒数，而且要**大于 0**（判定依据见
      `_channel_value_problem`）；
    - 枚举（`policy`）：取值表里没有就说不出口（`policy.POLICIES`）。
    """
    if key in _CHANNEL_BOOL:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        return None
    if key in _CHANNEL_NUMERIC:
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            return None
        return seconds if seconds > 0 else None
    if key in _CHANNEL_ENUM:
        text = str(value).strip()
        return text if text == "" or text in _CHANNEL_ENUM[key] else None
    return str(value)


def _channel_value_problem(key: str, raw) -> str:
    """`_coerce_channel_value` 认不出这个值时，**具体是哪一种认不出**。

    为什么要分开说：`timeout = "很久"` 与 `timeout = -5` 都落在同一句"这个值用不了"
    上，但下一步要改的地方完全不同——一个是类型写错了，另一个是没写正数。只说
    "读不成该有的类型"，第二种人会被指到错的方向去（诊断说假话比不说更坏）。

    `timeout` 的下界定在 **> 0** 的依据：它是"一轮最多等多少秒"，消费方是
    `bridge/agent_runner.py` 的 `deadline = now + timeout`——0 与负数意味着
    **每一轮当场就到点**，人看到的是"它说超过 -5 秒还没结束，先不等了"。同一仓里
    `tools/exec.py` 对命令超时的写法就是拒绝（"timeout 必须在 1 到 N 秒之间"），
    这里沿用同一条口径：认不出来就退回下一层，而不是把 -5 原样递给桥。
    """
    if key in _CHANNEL_NUMERIC:
        try:
            seconds = float(raw)
        except (TypeError, ValueError):
            return f"读不成数字（现在写的是 {raw!r}）"
        return f"要的是大于 0 的秒数（现在写的是 {seconds:g}）"
    if key in _CHANNEL_BOOL:
        return f"读不成真假（现在写的是 {raw!r}；写 true / false）"
    if key in _CHANNEL_ENUM:
        return (
            f"不在认得的取值里（现在写的是 {raw!r}；"
            f"可用：{'、'.join(_CHANNEL_ENUM[key])}）"
        )
    return f"读不成该有的类型（现在写的是 {raw!r}）"


def display_channel_value(value) -> str:
    """把一个通道值印成人看的样子。`spool qq` 的起手行与 `spool config` 共用。

    两条规矩，都是"印出来的字必须与生效值对得上"：

    - **只有"没给"（`None` / 空串）才印「（空）」**。原先起手行写的是
      `value or '（空）'`，那是按**真假**判空：`web = false` 印成「web=（空）」、
      `--timeout 0` 印成「timeout=（空）」，与"根本没给"在字面上不可区分——
      而这一行的全部意义就是"它到底读了哪一份"。文档写得对（`docs/bridge.md` 的
      样例是 `web=false(用户配置 …)`），错的是代码这边；
    - 真假值印 `true` / `false`：它照 TOML 的写法，与配置文件里能对得上号。
    """
    if value is None or value == "":
        return "（空）"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


@dataclass(frozen=True)
class ChannelValues:
    """一个通道算出来的默认值：**值、来源、以及说不出口的那些**。

    为什么把"来源"也带回来：这些值生效在别的进程里（桥、网页壳），出问题时
    唯一能回答"它到底用了哪一份"的就是这几个字。命令行那一层由调用方给，
    所以这里只说环境变量 / 用户配置 / 内置默认这三层。
    """

    name: str
    options: dict[str, object] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    #: 这一节里认不出的键名。**必须说出来**：写错一个字母的症状是
    #: "设置了却没生效"，而那种问题只靠复现是查不出来的。
    unknown: tuple[str, ...] = ()
    #: 值存在但**用不了**：读不成该有的类型（`timeout = "很久"`）、不在认得的
    #: 取值里（`policy = "AUTO"`）、或者不是正数（`timeout = -5`）。
    #: 用不了就退回下一层，并且**说出来**——静默跳过正是"设置了却没生效"。
    issues: tuple[str, ...] = ()

    def source(self, key: str) -> str:
        return self.sources.get(key, "未设置")

    def notes(self) -> str:
        """给人看的一两行：这一节里有什么不对。没问题时返回空串。"""
        lines = []
        if self.unknown:
            lines.append(
                f"[channel.{self.name}] 里这些键不认识（等于没配）："
                f"{'、'.join(self.unknown)}\n"
                f"    可用的键：{'、'.join(CHANNEL_KEYS)}"
            )
        for problem in self.issues:
            lines.append(f"[channel.{self.name}] {problem}")
        return "\n".join(lines)


def resolve_channel_options(
    name: str,
    overrides: dict[str, object] | None = None,
    env: dict[str, str] | None = None,
    path: Path | None = None,
) -> ChannelValues:
    """一个通道的默认值，按**命令行 > 环境变量 > 用户配置 > 内置默认**算出来。

    `overrides` 是命令行那一层：**给了的键（哪怕给的是空串）就到此为止**——
    命令行是"这一次的意图"，它压得住其余三层。"给没给"由调用方判定：
    短命令的 argparse 默认值是 `None`（空串在这里是合法取值，例如
    `--scope ""` 表示"不许自动落盘"）。

    环境变量的名字是 `SPOOLKIT_CHANNEL_<通道名>_<键>`（旧前缀
    `AGENTS_DEV_CHANNEL_<通道名>_<键>` 也认，新前缀优先），例如
    `SPOOLKIT_CHANNEL_QQBOT_PROXY`。新前缀优先这条与 `settings.resolve` 一致。

    每次调用都重读文件，不缓存：桥与网页壳是长驻进程，配置是会被改的。
    """
    override = dict(overrides or {})
    environment = os.environ if env is None else env
    section = load_channels(path).get(name) or {}
    unknown = tuple(sorted(str(key) for key in section if str(key) not in CHANNEL_KEYS))
    issues: list[str] = []

    values: dict[str, object] = {}
    sources: dict[str, str] = {}
    for key, builtin in CHANNEL_KEYS.items():
        if key in override:
            values[key] = override[key]
            sources[key] = "命令行"
            continue
        from_env = ""
        for prefix in (ENV_PREFIX, LEGACY_ENV_PREFIX):
            variable = prefix + CHANNEL_ENV_PREFIX + _channel_env_key(name, key)
            raw = (environment.get(variable) or "").strip()
            if raw:
                # 来源要报**实际用上的那个名字**：报了新前缀而人设的是旧前缀，
                # 他会去改一个本来就没设的变量。
                from_env, from_env_source = raw, f"环境变量 {variable}"
                break
        if from_env:
            converted = _coerce_channel_value(key, from_env)
            if converted is None:
                issues.append(
                    f"{key} 的环境变量 {from_env_source.removeprefix('环境变量 ')} "
                    f"{_channel_value_problem(key, from_env)}，这一层跳过"
                )
            else:
                values[key] = converted
                sources[key] = from_env_source
                continue
        if key in section:
            converted = _coerce_channel_value(key, section[key])
            if converted is None:
                issues.append(
                    f"{key} 的值{_channel_value_problem(key, section[key])}，这一层跳过"
                )
            else:
                values[key] = converted
                sources[key] = f"用户配置 [channel.{name}] {effective_config_path()}"
                continue
        values[key] = builtin
        sources[key] = "内置默认"
    return ChannelValues(
        name=name,
        options=values,
        sources=sources,
        unknown=unknown,
        issues=tuple(issues),
    )


def channel_options(name: str, path: Path | None = None) -> ChannelValues:
    """`[channel.<name>]` 这一段**本身**的默认值（不含命令行那一层）。

    `spool config` 与 `spool bridge --check` 用它回答"我配了没"；
    短命令用 `resolve_channel_options`（多一层命令行）。
    """
    return resolve_channel_options(name, path=path)


def known_channels(path: Path | None = None) -> list[str]:
    """用户配置里写了哪些通道小节（排序后）。没写就是空。"""
    return sorted(load_channels(path))


def resolve(key: str, flag: str = "", env: dict[str, str] | None = None) -> tuple[str, str]:
    """返回（值, 来源）。来源要能说出来——不然"我明明设了"就成了口头禅。"""
    if flag:
        return flag, "命令行"
    table = load()
    environment = os.environ if env is None else env
    for prefix in (ENV_PREFIX, LEGACY_ENV_PREFIX):
        from_env = environment.get(prefix + key.upper())
        if from_env:
            # 来源要报**实际用上的那个名字**：报了新前缀而人设的是旧前缀，
            # 他会去改一个本来就没设的变量。
            return from_env, f"环境变量 {prefix}{key.upper()}"
    if table.get(key):
        return table[key], f"配置文件 {effective_config_path()}"
    return "", "未设置"


def flag_enabled(key: str, flag: bool = False) -> bool:
    """开关：命令行 True 优先，否则看配置里像 true/1/yes/on 的值。默认关。"""
    if flag:
        return True
    value, _ = resolve(key)
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class McpServer:
    """一个外部 MCP 服务。配置写在用户配置文件的 `[[mcp]]` 里。

    为什么放在用户配置而不是工作区：它是"这台机器上有哪些外挂工具"，
    与人、与机器有关，与项目无关；而且工作区配置会跟着仓库提交，
    等于把某人的工具链塞给别人。
    """

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)


def load_mcp_servers(path: Path | None = None) -> list[McpServer]:
    """读 `[[mcp]]` 段。读不到、读坏了都返回空——外挂工具是增强，不该挡住启动。"""
    payload, _ = read_config(path)
    servers: list[McpServer] = []
    for item in payload.get("mcp") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        command = str(item.get("command") or "").strip()
        if not name or not command:
            continue
        args = item.get("args") or []
        env = item.get("env") or {}
        servers.append(
            McpServer(
                name=name,
                command=command,
                args=tuple(str(part) for part in args),
                env={str(k): str(v) for k, v in env.items()},
            )
        )
    return servers


@dataclass(frozen=True)
class ExternalService:
    """本机外接配方。写在用户配置的 `[[service]]` 里，不进仓库。

    `command` 可空：空就只探测、不替你启动。地址、gguf 路径、ssh 主机
    都由这台机器的人自己写，产品不绑死。常见名字的用途：`llama` 是本机
    语言模型；`tunnel` 是通道出网的 SOCKS（配合 `--bridge-proxy`），不是
    模型的 `--proxy`。
    """

    name: str
    command: tuple[str, ...] = ()
    health: str = ""


def _parse_command(raw) -> tuple[str, ...]:
    if raw is None or raw == "" or raw == []:
        return ()
    if isinstance(raw, str):
        import shlex

        return tuple(shlex.split(raw, posix=(os.name != "nt")))
    if isinstance(raw, list):
        return tuple(str(part) for part in raw)
    return ()


def load_services(path: Path | None = None) -> list[ExternalService]:
    """读 `[[service]]` 段。读不到就当没有——配方是增强，不该挡住启动。"""
    payload, _ = read_config(path)
    items: list[ExternalService] = []
    for item in payload.get("service") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        items.append(
            ExternalService(
                name=name,
                command=_parse_command(item.get("command")),
                health=str(item.get("health") or "").strip(),
            )
        )
    return items
