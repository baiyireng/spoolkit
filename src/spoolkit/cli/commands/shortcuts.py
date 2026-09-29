"""机器级入口的短命令：`spool web` 与 `spool qq`。

为什么值得两条短命令：这两个入口是**机器级**的——不给 `--root` 就不认 cwd，
它们服务整台机器，"你碰巧站在哪个目录"与"它服务谁"毫无关系。而"在任意目录
敲一下就能起手"这件事，**名字短本身就有一半功劳**：一条要在任意目录敲的命令，
不该同时要求你记住六个参数。

- `spool web` = `spool hub` 不带通道（网页壳；进去再选工作区）；
- `spool qq` = `spool bridge --channel qqbot …`，长参数全部有默认值，
  默认值来自用户配置 `[channel.qqbot]` 与三层配置。

`serve` / `hub` / `bridge` **全部保留、语义不变**：老脚本、`.bat`、文档都靠它们。
短命令只多加了"默认值"这一层，而且**只加在短命令自己身上**——`spool bridge`
的参数默认值今天是什么样，今天还是什么样（改它等于改既有行为，那是另一件事）。

命令行仍然压得住一切：短命令里的 argparse 默认值故意写成 `None`（"用户没表态"
这个第三取值），给了值就是"这一次的意图"，配置里那份盖不过它。
"""

import argparse
import sys

from spoolkit import settings
from spoolkit.cli.commands.bridge import bridge_command
from spoolkit.cli.commands.hub import hub_command

#: 短命令认得的那几个通道键，以及它们**落在哪个 argparse 参数上**。
#:
#: `proxy` 落的是 `bridge_proxy` 而不是 `proxy`，这一条必须写清楚：
#: `[channel.<名字>].proxy` 说的是"**通道自己**出网走哪条 SOCKS"，也就是
#: `--bridge-proxy`；而 `--proxy` 管的是**模型供应商**出网。写错成 `proxy`
#: 会让代理一路传进 agent 子进程、把模型请求也塞进那条隧道——那是两个完全
#: 不同的东西（文档里专门有一段讲这个区分）。
_CHANNEL_OPTION_KEYS: tuple[tuple[str, str], ...] = (
    ("proxy", "bridge_proxy"),
    ("policy", "policy"),
    ("scope", "scope"),
    ("timeout", "timeout"),
    ("web", "web"),
)
#: `root` 不走上面这张表：它由 `main()` 解析过，`--root` 给没给在那里已经定死了。


def _cli_overrides(args: argparse.Namespace) -> dict[str, object]:
    """命令行**真的给了**的那几个通道键，键名用通道配置里的名字。

    "给没给"必须按 `None` 判，不能按真假判：`--scope ""` 是一个合法取值
    （表示"不许自动落盘"），把它当成"没给"会让配置里的 `scope` 反过来盖住它——
    那就是"我明明在命令行上写了却没生效"。
    """
    overrides: dict[str, object] = {}
    for key, attr in _CHANNEL_OPTION_KEYS:
        value = getattr(args, attr, None)
        if value is None:
            continue
        overrides[key] = value
    return overrides


def _report_channel(values: settings.ChannelValues, *, stream=None) -> None:
    """把"这次用的是哪一份"打在起手的第一屏上。

    为什么值得打：这几个值生效在别的进程里（桥、网页壳），出问题时唯一能回答
    "它到底读了哪一份"的就是这几行。用户报的"我明明配了"里，绝大多数是**读的
    不是他改的那一份**。

    `stream` 的缺省必须是 `None` 而不是 `sys.stderr`：默认参数在**函数定义时**
    求值，写成 `sys.stderr` 就把它绑死在 import 那一刻的那个流上——而重定向、
    pytest 的 capsys、Web 壳都在**调用时**换掉 `sys.stderr`。绑死的后果是这几行
    跑到别处去了，恰恰是"起手第一屏看不见它"这个最难查的形状。
    """
    out = sys.stderr if stream is None else stream
    notes = values.notes()
    if notes:
        print(notes, file=out)
    # 只报**这一层真的应用了的**那几个键（`_CHANNEL_OPTION_KEYS`）：这行写的是
    # "本次生效"，把没生效的键印成生效值，就正是这套东西要消灭的那种病
    # （"终端说用它、实际跑另一条"）。`root` 因此不在这一行里——它由 `main()`
    # 解析（`--root` 给没给在那里已经定死），短命令不从配置里补它。
    # 值一律**按值本身印**（`settings.display_channel_value`）：空值印「（空）」，
    # 但 `web = false` 必须印 `false`、`--timeout 0` 必须印 `0.0`——按真假判空
    # 会把这两个真实取值印成「（空）」，与"根本没给"在字面上不可区分
    # （`docs/bridge.md` 的样例写的是 `web=false(用户配置 …)`，那才是对的）。
    print(
        f"[channel.{values.name}] 本次生效："
        + "、".join(
            f"{key}={settings.display_channel_value(values.options[key])}"
            f"({values.source(key)})"
            for key, _ in _CHANNEL_OPTION_KEYS
        ),
        file=out,
    )


def _fill_from_channel(args: argparse.Namespace, name: str) -> settings.ChannelValues:
    """按三层配置补齐短命令的参数，并返回算出来的那一组值。

    命令行给了的键**原样留在 `args` 上**（哪怕给的是空串），其余从环境变量 /
    用户配置 / 内置默认补。补完再写回 `args`，好让下游（桥、网页壳）看到的
    都是**生效值**——否则桥自己再解析一遍，症状是"终端说用它、实际跑另一条"。
    """
    values = settings.resolve_channel_options(name, _cli_overrides(args))
    _report_channel(values)
    for key, attr in _CHANNEL_OPTION_KEYS:
        setattr(args, attr, values.options[key])
    return values


def web_command(args: argparse.Namespace) -> int:
    """`spool web`：机器级网页壳（= `hub` 不带通道）。

    等价关系是逐字的：这里只做两件事——把没表态的 `--web` 落回"默认开"
    （与 `serve` / `hub` 的 `--web` 默认值同口径），然后把整个 `args` 交给
    `hub_command`。所以 `hub` 有的参数它都有（共用同一份参数定义），`hub` 的
    行为它一步不差。

    网页壳**没有**"第二层配置"：`[channel.<名字>]` 是按通道名来的，而这条命令
    的契约就是不带通道（下面那行把 `channel` 定成空串），所以它读不了那一节。
    它也不读 cwd——机器级入口的目标由网页壳自己的指向决定。

    `channel` 显式定成空串：`spool web` 的契约就是"**不带通道**的机器级网页壳"
    （设计 §3.5 的等价表）。想连通道用 `spool hub --channel qqbot` 或 `spool qq`。
    """
    args.channel = ""
    if getattr(args, "web", None) is None:
        # 用户没表态，而 `[channel.<名字>]` 里也没有这个键（网页壳没有通道名，
        # 读不了那一节）：回到"**默认开**"。这一条必须与 `serve` / `hub` 的
        # `--web` 默认值同口径——短命令的契约就是"等价于 hub 不带通道"。
        # 写成 False 的后果不是"少一个开关"，而是网页壳在没人要求的情况下
        # **静默**少掉一整块联网能力（`--no-web` 会一路转到子进程上）。
        args.web = True
    return hub_command(args)


def qq_command(args: argparse.Namespace) -> int:
    """`spool qq`：按用户配置起 QQ 通道（= `bridge --channel qqbot …`）。

    长参数全部有默认值：`proxy` / `policy` / `scope` / `timeout` / `web` 来自
    `[channel.qqbot]`（三层配置：命令行 > 环境变量 > 用户配置 > 内置默认）。
    凭据不走这里——它在桥那一步按 `resolve_secret` 解析，机器级桥只认用户级
    `.env`（`spool env` 写的就是那一份）。

    `--root` 显式给了就钉住那一个工作区（照旧）；不给就是机器级：落到哪条聊天
    的指向决定，不看 cwd。
    """
    name = str(getattr(args, "channel", "") or "").strip() or "qqbot"
    args.channel = name
    _fill_from_channel(args, name)
    return bridge_command(args)
