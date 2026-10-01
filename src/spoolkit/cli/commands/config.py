"""用户级默认配置：看、设、清。

为什么把它做成一条命令而不是让人去改文件：**"现在到底用的哪个 provider"
必须一眼看得到，而且要看得到它是从哪来的**——命令行、环境变量、还是配置
文件。看不到来源时，症状永远是"我明明设了"，然后开始怀疑代码。
"""

import argparse
import sys

from spoolkit import settings


def _render(effective: dict[str, str], path) -> str:
    lines = [f"配置文件：{path}" + ("" if path.is_file() else "（尚未创建）"), ""]
    lines.append(f"{'键':<10}{'生效值':<34}来源")
    lines.append("-" * 70)
    for key in settings.KEYS:
        value = effective.get(key) or "（未设置）"
        source = effective[f"{key}_source"]
        if len(value) > 32:
            value = value[:29] + "…"
        lines.append(f"{key:<10}{value:<34}{source}")
    lines.append("")
    lines.append("优先级：命令行 > 环境变量 SPOOLKIT_<键名大写> > 配置文件")
    lines.append("（改名前的 AGENTS_DEV_<键名大写> 也认，新前缀优先）")
    lines.append("设置：spool config --set provider=llamacpp")
    lines.append("清除：spool config --reset provider")
    return "\n".join(lines)


#: 短命令**不读**的那些通道键，以及为什么——`spool config` 必须标出来。
#:
#: 这一节印的形状与"生效值"那几行一模一样，于是 `[channel.qqbot]` 里写了
#: `root = "D:\某个工作区"` 的人会以为它生效；而短命令**根本不读它**：工作区是在
#: `main()` 那一步解析的，"没给 `--root`"（机器级）与"给了空串"到那里已经是同一个
#: 值，回头再拿配置补就等于改掉"服务整台机器"这条契约（`docs/bridge.md` 里那段）。
#: 只标不删：删掉这一行，配了它的人连"我配过"都看不见了——那比标出来更坏。
#: 这张表的键必须与"短命令实际读的那几个"互补，有测试钉住（`tests/cli/test_shortcuts.py`）。
_NOT_READ_BY_SHORTCUTS = {
    "root": "   ← 短命令不读取此项（要固定工作区用 --root）",
}


def _render_channels() -> list[str]:
    """`[channel.<名字>]` 那几节现在是什么（短命令 `spool qq` 用的就是它）。

    为什么值得印：这一节是**人手写进 TOML 的**（`--set` 管不到子表），于是它是
    全仓里唯一一处"配错了不会有任何反馈"的地方——写错一个键名，症状就是
    `spool qq` 的某个参数无提示地回到默认值。印出来 + 把认不出的键指出来，
    是这里唯一能给的反馈。
    """
    names = settings.known_channels()
    if not names:
        return []
    lines = ["", "通道默认值（短命令 spool qq / 网页壳用；命令行仍可覆盖）："]

    for name in names:
        values = settings.channel_options(name)
        lines.append(f"  [channel.{name}]（{settings.effective_config_path()}）")
        for key in settings.CHANNEL_KEYS:
            # 值照印：这一节是**非密钥**（密钥走 `spool env`），而"我明明配了"的唯一
            # 解药就是看见它现在到底是多少。形状与 `spool qq` 起手那一行**共用同一份**
            # （`settings.display_channel_value`），否则两处会各印各的。
            # 值比列宽长时**至少留一个空格**再接来源：`root` 的值是个路径，印成
            # `D:\某个工作区用户配置 [channel.qqbot] …` 谁也读不出来。
            # 短命令不读的键（`root`）后面还跟着一句话，免得它被当成生效值。
            value = settings.display_channel_value(values.options[key])
            lines.append(
                f"    {key:<8}{value}{' ' * max(1, 10 - len(value))}"
                f"{values.source(key)}{_NOT_READ_BY_SHORTCUTS.get(key, '')}"
            )
        notes = values.notes()
        if notes:
            lines.extend(f"  ⚠ {line}" for line in notes.splitlines())
    return lines


def config_command(args: argparse.Namespace) -> int:
    path = settings.config_path()
    _, problem = settings.read_config(path)
    if problem:
        print(problem, file=sys.stderr)
    table = settings.load(path)
    changed = False

    for item in getattr(args, "set", []) or []:
        if "=" not in item:
            print(f"需要写成 键=值：{item}", file=sys.stderr)
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
    channels = _render_channels()
    if channels:
        print("\n".join(channels))
    return 0
