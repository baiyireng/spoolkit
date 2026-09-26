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
    """写白名单里的键，并保留 `[[mcp]]` / `[[service]]` 以及白名单以外的
    顶层标量（例如人手写的 `tavily_api_key`）。

    数组表不在 KEYS 里；以前整文件重写会把它们抹掉——
    `spool config --set` 一次，外挂工具、本机配方和搜索 key 就丢了。
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
        if key in KEYS or key in ("mcp", "service"):
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
