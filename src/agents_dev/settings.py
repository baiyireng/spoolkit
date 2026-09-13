"""用户级默认配置。

为什么需要它：配置有三层，缺一层就会别扭——

- **工作区级**（`.agent/limits.json`、`.agent/policy.json`）：属于这个项目，
  跟着项目走，已经齐了；
- **命令行**：一次一给，用于临时覆盖；
- **用户级**（本模块）：属于这台机器上的你。缺了它，同一件事每天都要重打一遍
  `--provider llamacpp --base-url http://127.0.0.1:8080`——而这类重复输入最后
  的下场是被人做成一个 .bat，然后没人知道真正的配置在哪。

优先级：**命令行 > 环境变量 > 用户配置 > 内置默认**。顺序不能反：命令行是
本次运行的意图，环境变量是这次 shell 的意图，配置文件是长期默认。

文件位置：`AGENTS_DEV_CONFIG` 指到哪就用哪；否则 Windows 用
`%APPDATA%\\agents-dev\\config.toml`，其它平台用 `~/.config/agents-dev/config.toml`。
**不放在工作区里**：工作区是要提交、要分享的东西，不该承载某台机器的默认值。
"""

import os
import sys
import tomllib
from pathlib import Path

# 能配置的键。写成一个白名单而不是"想存什么存什么"：
# 配置文件的键写错一个字母，症状是"设置了但没生效"，而那种问题最难查。
KEYS: tuple[str, ...] = ("provider", "model", "base_url", "proxy", "script")

# 对应的环境变量名：AGENTS_DEV_<KEY 大写>。
ENV_PREFIX = "AGENTS_DEV_"


def config_path() -> Path:
    """配置文件位置。`AGENTS_DEV_CONFIG` 优先（测试与多套配置用）。"""
    override = os.environ.get("AGENTS_DEV_CONFIG")
    if override:
        return Path(override)
    if sys.platform == "win32":
        root = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "agents-dev" / "config.toml"


def load(path: Path | None = None) -> dict[str, str]:
    """读配置。文件不存在或读坏了都返回空——配置是增强，不该挡住启动。"""
    target = path or config_path()
    if not target.is_file():
        return {}
    try:
        payload = tomllib.loads(target.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return {
        key: str(payload[key])
        for key in KEYS
        if isinstance(payload.get(key), (str, int, float))
    }


def save(values: dict[str, str], path: Path | None = None) -> Path:
    """写配置。只写白名单里的键，其余忽略。"""
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# agents-dev 的用户级默认配置。命令行与环境变量优先于这里。",
        "# 看当前生效值：agents-dev config",
        "",
    ]
    for key in KEYS:
        if key in values and values[key] != "":
            lines.append(f'{key} = "{values[key]}"')
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def resolve(key: str, flag: str = "", env: dict[str, str] | None = None) -> tuple[str, str]:
    """返回（值, 来源）。来源要能说出来——不然"我明明设了"就成了口头禅。"""
    if flag:
        return flag, "命令行"
    table = load()
    environment = os.environ if env is None else env
    from_env = environment.get(ENV_PREFIX + key.upper())
    if from_env:
        return from_env, f"环境变量 {ENV_PREFIX}{key.upper()}"
    if table.get(key):
        return table[key], f"配置文件 {config_path()}"
    return "", "未设置"
