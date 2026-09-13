"""用户级默认配置：看、设、清。

为什么把它做成一条命令而不是让人去改文件：**"现在到底用的哪个 provider"
必须一眼看得到，而且要看得到它是从哪来的**——命令行、环境变量、还是配置
文件。看不到来源时，症状永远是"我明明设了"，然后开始怀疑代码。
"""

import argparse
import sys

from agents_dev import settings


def _render(effective: dict[str, str], path) -> str:
    lines = [f"配置文件：{path}" + ("" if path.is_file() else "（还没有）"), ""]
    lines.append(f"{'键':<10}{'生效值':<34}来源")
    lines.append("-" * 70)
    for key in settings.KEYS:
        value = effective.get(key) or "（未设置）"
        source = effective[f"{key}_source"]
        if len(value) > 32:
            value = value[:29] + "…"
        lines.append(f"{key:<10}{value:<34}{source}")
    lines.append("")
    lines.append("优先级：命令行 > 环境变量 AGENTS_DEV_<键名大写> > 配置文件")
    lines.append("设置：agents-dev config --set provider=llamacpp")
    lines.append("清除：agents-dev config --reset provider")
    return "\n".join(lines)


def config_command(args: argparse.Namespace) -> int:
    path = settings.config_path()
    _, problem = settings.read_config(path)
    if problem:
        print(problem, file=sys.stderr)
    table = settings.load(path)
    changed = False

    for item in getattr(args, "set", []) or []:
        if "=" not in item:
            print(f"要写成 键=值 的形式：{item}", file=sys.stderr)
            return 2
        key, _, value = item.partition("=")
        key = key.strip()
        if key not in settings.KEYS:
            print(
                f"没有这个键：{key}（可用：{'、'.join(settings.KEYS)}）",
                file=sys.stderr,
            )
            return 2
        table[key] = value.strip()
        changed = True

    for key in getattr(args, "reset", []) or []:
        if key not in settings.KEYS:
            print(f"没有这个键：{key}", file=sys.stderr)
            return 2
        table.pop(key, None)
        changed = True

    if changed:
        written = settings.save(table, path)
        print(f"已写入 {written}")

    if getattr(args, "show_path", False):
        print(path)
        return 0

    # 生效值仍然要按优先级算一遍：配置文件里有值，不代表它现在生效
    # （环境变量或命令行可能盖过它）。
    effective = {}
    for key in settings.KEYS:
        value, source = settings.resolve(key)
        effective[key] = value
        effective[f"{key}_source"] = source
    print(_render(effective, path))
    return 0
