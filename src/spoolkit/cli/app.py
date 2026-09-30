"""命令行入口。

这里只做两件事：定义参数、把命令分发到对应实现。
装配细节在 runtime.py，选项决策在 options.py，各条命令在 commands/ 下。

保持入口文件薄是有原因的：它曾经有近 800 行，把参数解析、装配、
三条执行路径全混在一起，结果一个闭包变量绑定错误藏了整整一轮才被发现——
文件太长时，读不完就看不出来。
"""

import argparse
import sys
from pathlib import Path

from spoolkit import __version__
from spoolkit import onboarding
from spoolkit import settings
from spoolkit import status as status_report
from spoolkit import workspace
from spoolkit.bench import DEFAULT_BENCH_ROOT
from spoolkit.cli.commands.bench import bench
from spoolkit.cli.commands.plan import make_plan
from spoolkit.cli.commands.policy import policy_command
from spoolkit.cli.commands.revert import revert_command
from spoolkit.cli.commands.run import run
from spoolkit.cli.commands.serve import serve_command
from spoolkit.cli.commands.hub import hub_command
from spoolkit.cli.commands.shortcuts import qq_command, web_command
from spoolkit.cli.commands.diagnose import diagnose_command
from spoolkit.cli.commands.session import session_command
from spoolkit.cli.commands.ws import ws_command
from spoolkit.cli.commands.limits import limits_command
from spoolkit.cli.commands.config import config_command
from spoolkit.cli.commands.env import env_command
from spoolkit.cli.commands.chat import chat_command
from spoolkit.cli.commands.bridge import approve_entry, bridge_command
from spoolkit.cli.commands.init import init_command
from spoolkit.cli.commands.extra import extra_command
from spoolkit.cli.commands.service import service_command
from spoolkit.cli.commands.status import status_command
from spoolkit.cli.commands.help import help_command
from spoolkit.cli.commands.mcp import mcp_command
from spoolkit.cli.commands.mcp_servers import mcp_servers_command
from spoolkit.cli.options import (
    DEFAULT_WINDOW,
    report_policy,
    resolve_policy,
    resolve_scope,
    resolve_window,
)
from spoolkit.cli.runtime import (
    LoopWiring,
    PREFETCH_BUDGET,
    assemble_loop,
    build_approver,
    build_lessons,
    build_loop,
    build_memory,
    open_memory,
    resolve_provider_args,
    settle_lessons,
    show_history,
)
from spoolkit.llm.providers import describe_providers, provider_names
from spoolkit.policy import POLICIES

# 这些名字以前直接定义在 app.py 里，外部（含测试）按此路径导入。
# 拆文件之后在这里重新导出，避免留下断裂的导入路径。
__all__ = [
    "DEFAULT_WINDOW",
    "LoopWiring",
    "PREFETCH_BUDGET",
    "assemble_loop",
    "build_approver",
    "build_lessons",
    "build_loop",
    "build_memory",
    "main",
    "open_memory",
    "report_policy",
    "resolve_policy",
    "resolve_scope",
    "resolve_window",
    "settle_lessons",
    "show_history",
]


def _add_provider_args(parser: argparse.ArgumentParser, default: str) -> None:
    """供应商相关参数。

    `default` 只是**名义上的**兜底：真正生效的值由 `resolve_provider_args`
    按「命令行 > 环境变量 > 用户配置 > 内置默认」算出来。所以这里的 argparse
    默认值必须是空串——写成 "fake"/"gemini" 的话，解析完就成了一次"显式指定"，
    用户在 `spool config` 里设的默认永远盖不过它。
    """
    del default  # 保留形参只为让调用点读起来清楚
    parser.add_argument(
        "--provider",
        "--engine",
        dest="provider",
        choices=provider_names(),
        default="",
        help="模型供应商；" + describe_providers(),
    )
    parser.add_argument("--model", default="", help="留空则用供应商默认模型")
    parser.add_argument("--base-url", default="", help="llama.cpp 服务地址")
    parser.add_argument("--proxy", default="", help="留空则使用系统代理")
    parser.add_argument("--root", default=None, help="工作区路径（默认自动找）")
    parser.add_argument(
        "--no-setup",
        action="store_true",
        help="第一次运行时不要弹配置向导（脚本/CI 里用）",
    )
    parser.add_argument(
        "--no-mcp",
        action="store_true",
        help="不挂用户配置里的 MCP 外挂工具（排查「是不是外挂在捣乱」时用）",
    )


def _add_run_command(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("run", help="运行一次任务")
    parser.add_argument("--goal", default="")
    _add_provider_args(parser, default="fake")
    parser.add_argument(
        "--script",
        default="",
        help="仅测试回放：fake 的应答脚本 JSON。fake 不能写代码，真实任务不要走这条",
    )
    parser.add_argument("--window", type=int, default=0, help="0 表示自动向供应商查询")
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument(
        "--step-ceiling",
        type=int,
        default=0,
        help="总步数上限（含督导给的续期）；0 表示按基础预算自动算",
    )
    parser.add_argument(
        "--no-supervise",
        action="store_true",
        help="关掉督导：撞上步数上限就停，由你自己决定要不要 --resume",
    )
    parser.add_argument(
        "--max-targets",
        type=int,
        default=0,
        help="一次派发最多带几件（0 表示用默认值 10）。强模型/远程 API 可以调大",
    )
    parser.add_argument(
        "--review-limit",
        type=int,
        default=0,
        help="超过几件就提醒「审查会吃力」（0 表示用默认值 5）",
    )
    parser.add_argument(
        "--subagent-steps",
        type=int,
        default=20,
        help="子智能体的步数上限，默认比主循环大（一次性容器，重派成本低）",
    )
    parser.add_argument("--no-memory", action="store_true", help="关闭记忆读写")
    parser.add_argument(
        "--delegate", action="store_true", help="先判断是否派发；派发则由实现者做、审查者验"
    )
    parser.add_argument("--plan", action="store_true", help="执行计划中的下一个待办步骤")
    parser.add_argument(
        "--autonomous", action="store_true", help="自己拆解目标并逐步做完；必须给 --scope"
    )
    parser.add_argument(
        "--resume", action="store_true", help="接着上次未完成的检查点继续"
    )
    parser.add_argument(
        "--policy",
        choices=POLICIES,
        default="",
        help="本次运行的授权策略；留空则用已保存的设置",
    )
    parser.add_argument(
        "--scope", default="", help="auto 策略下允许自动落盘的路径，逗号分隔"
    )
    parser.add_argument("--limit", type=int, default=10, help="自主模式下最多拆几步")
    parser.add_argument(
        "--batch",
        type=int,
        default=0,
        help=(
            "分批推进：每批排几步。给了它就变成「先做一批 → 回头看一眼 → "
            "再排下一批」，后续步骤能吃到前面的实际结果。0 表示一次排完"
        ),
    )
    parser.add_argument(
        "--no-roll",
        action="store_true",
        help="即使给了 --batch 也一次排完（对照用）",
    )
    parser.add_argument(
        "--cover",
        default="",
        help=(
            "拆解后要检查覆盖的清单（逗号分隔）。目标明说「把这 N 件事都做完」"
            "时才给；不给就不检查——什么算「必须覆盖」是关于目标的判断，"
            "不该由 harness 猜"
        ),
    )
    parser.add_argument(
        "--session",
        default="",
        help="会话名。留空 = 用指针里那条，没有就用老默认名 cli",
    )
    parser.add_argument(
        "--history", type=int, default=6, help="启动时显示最近几轮会话记录"
    )
    parser.add_argument("--no-history", action="store_true", help="不显示会话记录")
    parser.add_argument(
        "--events",
        action="store_true",
        help="以 JSON 行输出事件，供 Web UI 消费；此模式下不打印散文",
    )
    parser.add_argument(
        "--from-channel",
        action="store_true",
        help="远程聊天通道：只限制文件读写（当前工作区）；查资料不在这条限制里",
    )
    parser.add_argument(
        "--ask-on-stdin",
        action="store_true",
        help=(
            "events 模式下允许在 stdin 上问授权（桥/网页壳用：它们会把问题转给"
            "用户、再把回答写回来）。不给就是无人值守，一律拒绝"
        ),
    )
    _add_web_args(parser)
    parser.add_argument(
        "--allow-read",
        action="append",
        default=[],
        metavar="目录",
        help=(
            "授权额外可读目录（可重复，一次性，不写进配置）。"
            "要持久关联用 `spool extra add`；写入仍要确认"
        ),
    )
    parser.set_defaults(func=run)


def _add_web_args(parser: argparse.ArgumentParser, default: bool | None = True) -> None:
    """联网取用（research / web_fetch / web_read）。

    **默认开**：QQ、网页壳、CLI 同一套能力。网页内容仍按不可信资料处理
    （里面的「指令」不是用户指令；内网地址硬拦）。不要就 `--no-web`。
    白名单/黑名单/上限一律由调用方给，这里不写死。

    `default=None` 是给**短命令**（`web` / `qq`）用的第三个取值：
    "用户没表态"。它们的默认值来自用户配置 `[channel.<名字>]`，而 argparse
    只要写了 `default=True`，解析完就成了一次"显式给的"，配置里那个
    `web = false` 永远盖不过它——这正是"设置了却没生效"的形状。
    """
    parser.add_argument(
        "--web",
        action=argparse.BooleanOptionalAction,
        default=default,
        help="联网取用（默认开）：research 查资料；--no-web 关掉",
    )
    parser.add_argument(
        "--web-allow",
        action="append",
        default=[],
        metavar="域名",
        help="只允许抓这些域名（可重复；不给=允许任何公网域名，内网仍拦）",
    )
    parser.add_argument(
        "--web-deny",
        action="append",
        default=[],
        metavar="域名",
        help="禁止抓这些域名（可重复，优先于 --web-allow）",
    )
    parser.add_argument(
        "--web-search-api",
        default="",
        metavar="名字",
        help="网页搜索后端（现在只接 tavily）。不给但有 Tavily key 就当 tavily；没 key=没配搜索",
    )


def _add_plan_command(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("plan", help="把目标拆成可验收的步骤序列")
    parser.add_argument("--goal", required=True)
    # 计划是会话级的（见 session_state.py）：这里的默认值必须与 `run` 一致，
    # 否则 `spool plan` 排的计划，`spool run --plan` 找不到。现在两者都是空串，
    # 由内核按"显式 > 指针 > cli"定，仍然是同一条。
    parser.add_argument("--session", default="", help="会话名，默认与 run 同一个会话")
    _add_provider_args(parser, default="gemini")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--cover",
        default="",
        help="拆解后要检查覆盖的清单（逗号分隔），见 run --help",
    )
    parser.set_defaults(func=make_plan)


def _add_policy_command(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("policy", help="查看或调整授权策略")
    parser.add_argument("--set", dest="new_policy", choices=POLICIES, default="")
    parser.add_argument("--root", default=None, help="工作区路径（默认自动找）")
    parser.set_defaults(func=policy_command)


def _add_revert_command(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("revert", help="回滚上一次写入的改动")
    parser.add_argument("--root", default=None, help="工作区路径（默认自动找）")
    parser.set_defaults(func=revert_command)


def _add_bench_command(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("bench", help="跑回归任务集")
    _add_provider_args(parser, default="gemini")
    parser.add_argument("--tasks", default=DEFAULT_BENCH_ROOT)
    parser.add_argument("--filter", default="", help="只跑名字含该片段的任务")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="只跑前 N 道题（长任务那条仪器一次要跑几十分钟，先用前 N 道看形状）",
    )
    parser.add_argument("--window", type=int, default=0)
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument(
        "--step-ceiling",
        type=int,
        default=0,
        help="总步数上限（含督导给的续期）；0 表示按基础预算自动算",
    )
    parser.add_argument(
        "--together",
        action="store_true",
        help="第二条仪器：整批题铺进一个工作区，全部交给一条会话（量长任务与派发）",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="打印每个任务的轨迹与验收输出"
    )
    parser.set_defaults(func=bench)


def _add_session_command(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("session", help="会话管理：列 / 建 / 切 / 改名 / 归档 / 查历史")
    parser.add_argument("--root", default=None, help="工作区路径（默认自动找）")
    parser.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=["list", "new", "use", "rename", "archive", "search"],
    )
    parser.add_argument(
        "name", nargs="?", default="", help="会话 id / 序号 / latest；`search` 时是关键词"
    )
    parser.add_argument("title", nargs="?", default="", help="`new` 或 `rename` 时的标题")
    parser.add_argument("--all", action="store_true", help="连归档的一起列")
    parser.add_argument(
        "--all-sessions",
        dest="all_sessions",
        action="store_true",
        help="`search` 跨会话：查本工作区的全部会话（不给就只查当前会话）",
    )
    parser.add_argument(
        "--from-session",
        dest="from_session",
        default="",
        help="`search` 只看这一条会话（不给 = 当前会话；`--all-sessions` 优先）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="`search` 最多几条（`0` = 按标定值 history_search_limit）",
    )
    parser.add_argument(
        "--since", default="", help="`search` 只看这个时间起（2026-09-27 或 2026-09-27 14:03）"
    )
    parser.add_argument(
        "--until", default="", help="`search` 只看这个时间止；只给日期按当天 00:00 算"
    )
    parser.set_defaults(func=session_command)


def _add_env_command(sub: argparse._SubParsersAction) -> None:
    """`spool env`：用户级 `.env`（密钥那一层）。

    **不带 `--root`**：这条命令管的是"这台机器上的你"，与在哪个工作区无关。
    """
    parser = sub.add_parser(
        "env", help="用户级 .env：看位置与键名 / 写入 / 从工作区搬"
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=["list", "path", "set", "import"],
    )
    parser.add_argument(
        "items", nargs="*", default=[], help="`set` 时给 KEY=VALUE，可给多个"
    )
    parser.add_argument(
        "--from", dest="source", default="", help="`import` 时的来源 .env"
    )
    parser.add_argument(
        "--only", action="append", default=[], help="`import` 时只搬这些键，可给多次"
    )
    parser.set_defaults(func=env_command)


def _add_ws_command(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "ws", aliases=["workspace"], help="工作区管理：列 / 切 / 加 / 忘 / 别名"
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=["list", "use", "add", "forget", "alias"],
    )
    parser.add_argument("name", nargs="?", default="", help="名字、序号、路径或别名")
    parser.add_argument("value", nargs="?", default="", help="`alias` 时给别名")
    parser.set_defaults(func=ws_command)
    # 注意：`ws` 自己**不带** --root，它是用来选工作区的，不是被解析的对象。


def _add_serve_parser(
    sub: argparse._SubParsersAction,
    name: str,
    help_text: str,
    *,
    func,
    web_default: bool | None = True,
    description: str = "",
) -> argparse.ArgumentParser:
    """`serve` / `hub` / `web` 共用的那组参数（三者的参数面本来就一样）。

    放在一处是因为**参数面分叉的症状是"这条命令能设、那条不能"**：短命令
    `spool web` 的契约就是"等价于 `spool hub` 不带通道"，它多一个少一个参数
    都算说话不算数。`qq` 不在这里——它的参数面是 `bridge` 那一套。

    `description` 与 `help_text` 是两处地方：`help_text` 只在 `spool --help`
    那张命令表里出现，`description` 才是 `spool web --help` 自己的第一屏。
    短命令要靠后者说清"我是机器级的、任意目录可用"，否则人得先读设计文档。
    """
    parser = sub.add_parser(name, help=help_text, description=description or None)
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="监听地址（默认只绑本机）。绑非回环地址（如 0.0.0.0）时是**只读**壳："
        "能看页面与事件，/run 与 /confirm 返回 403（本批不实现远程可写）",
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--session", default="", help="会话名。留空 = 用网页壳的指针，没有就 cli"
    )
    parser.add_argument("--provider", default="")
    parser.add_argument("--model", default="")
    parser.add_argument(
        "--script",
        default="",
        help="仅测试回放：fake 的应答脚本。fake 不能写代码，真实任务不要走这条",
    )
    parser.add_argument("--base-url", default="", help="llama.cpp 服务地址")
    parser.add_argument("--proxy", default="")
    parser.add_argument("--policy", default="")
    parser.add_argument("--scope", default="")
    parser.add_argument(
        "--allow-read",
        action="append",
        default=[],
        metavar="目录",
        help="授权额外可读目录（可重复），转发给子进程",
    )
    _add_web_args(parser, default=web_default)
    parser.add_argument("--root", default=None, help="工作区路径（默认自动找）")
    # `--root` 不给时不要替它自动找一个：留给内核按"当前目录 → 网页壳指针 →
    # 登记表"退。少了这一条，从任何工作区目录敲 `spool serve` 都会被填成
    # `explicit`，`client="web"` 那层指向永远读不到（Task 6 评审的 Important-1）。
    # `web_default` 只用来给 `--web` 定默认值，不进 Namespace：留一个没人读的
    # 属性在 `args` 上，下一个读它的人会以为它管着什么。
    parser.set_defaults(func=func, optional_root=True)
    return parser


def _add_serve_command(sub: argparse._SubParsersAction) -> None:
    _add_serve_parser(sub, "serve", "启动 Web UI 壳", func=serve_command)


def _add_web_command(sub: argparse._SubParsersAction) -> None:
    """`spool web`：机器级网页壳的短命令（= `hub` 不带通道）。

    为什么值得一个短命令：网页壳是**机器级**的（不给 `--root` 就不认 cwd，
    进去再选工作区），可它的名字却叫 `hub`——而"在任意目录敲一下就能起手"
    这件事，短本身就有一半功劳。`hub` 原样保留、语义不变。
    """
    _add_serve_parser(
        sub,
        "web",
        "起机器级网页壳（= hub 不带通道；任意目录可用，进去再选工作区）",
        func=web_command,
        web_default=None,
        description=(
            "机器级网页壳：**任意目录**都能起（不认 cwd），不给 --root 就不钉工作区，"
            "进去再选工作区与会话。等价于 `spool hub` 不带 --channel 的那一半；"
            "hub 有的参数它都有。要连聊天通道就用 `spool hub --channel …` 或 `spool qq`。"
        ),
    )


def _add_hub_command(sub: argparse._SubParsersAction) -> None:
    """产品内核：网页壳 + 可选 bridge。不启 llama / 隧道。"""
    parser = _add_serve_parser(
        sub,
        "hub",
        "起网页壳；声明了通道才再拉 bridge（不启 llama-server / 隧道）",
        func=hub_command,
    )
    parser.add_argument(
        "--channel",
        default="",
        help="可选：再拉一条 bridge（fake / telegram / wecom / qqbot）。不给就只起网页壳",
    )
    parser.add_argument(
        "--access",
        choices=("pairing", "allowlist", "open"),
        default="pairing",
        help="通道准入；只在给了 --channel 时有意义",
    )
    parser.add_argument(
        "--allow-user",
        action="append",
        default=[],
        metavar="用户",
        help="通道白名单（可重复）；给了就切到名单模式",
    )
    parser.add_argument(
        "--bridge-proxy",
        default="",
        metavar="地址",
        help=(
            "通道自己出网走哪个代理（如 socks5://127.0.0.1:1080）。"
            "本机 [[service]] 里名叫 tunnel 的配方通常就是这条 SOCKS；"
            "和 --proxy（模型供应商）不是一回事"
        ),
    )
    parser.add_argument("--token", default="", help="Telegram bot token（或用环境变量）")
    parser.add_argument("--appid", default="", help="QQ 机器人的 AppID（或用环境变量）")
    parser.add_argument("--secret", default="", help="QQ 机器人的 AppSecret（或用环境变量）")
    parser.add_argument(
        "--sandbox", action="store_true", help="QQ 机器人用沙箱环境"
    )
    parser.add_argument(
        "--notify-disconnect",
        action="store_true",
        help="模型/外接服务从通变断时，给最近聊天发一句（默认关，避免刷屏）",
    )
    # `optional_root` 与 func 已由 `_add_serve_parser` 设好，这里不再重复。


def _add_qq_command(sub: argparse._SubParsersAction) -> None:
    """`spool qq`：按用户配置起 QQ 官方机器人通道。

    **长参数全部有默认值**（`[channel.qqbot]` + 三层配置），命令行仍可覆盖；
    凭据走用户级 `.env`（机器级入口不认 cwd）。它就是"在 `C:\\Users\\32615`
    直接敲一下就能起来"的那条命令——用户真这么报过一次，而当时要敲六个参数。
    """
    parser = sub.add_parser(
        "qq",
        help="起 QQ 官方机器人通道（= bridge --channel qqbot；默认值来自用户配置 [channel.qqbot]）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="一轮任务最多等多少秒；不给则用 [channel.qqbot] 里的 timeout，再不然 900",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只看凭据与连通性，不起通道（等价于 bridge --check）",
    )
    parser.add_argument(
        "--bridge-proxy",
        default=None,
        metavar="地址",
        help="通道自己出网走哪个代理；不给则用 [channel.qqbot] 的 proxy",
    )
    parser.add_argument("--token", default="", help="（QQ 用不到；留给通道插件）")
    parser.add_argument("--appid", default="", help="QQ 机器人的 AppID（或用用户级 .env）")
    parser.add_argument("--secret", default="", help="QQ 机器人的 AppSecret（或用用户级 .env）")
    parser.add_argument(
        "--sandbox", action="store_true", help="QQ 机器人用沙箱环境（sandbox.api.sgroup.qq.com）"
    )
    parser.add_argument(
        "--access",
        choices=("pairing", "allowlist", "open"),
        default="pairing",
        help="通道准入：pairing（默认）/ allowlist（只放行 --allow-user）/ open（谁都行）",
    )
    parser.add_argument(
        "--allow-user",
        action="append",
        default=[],
        metavar="用户",
        help="白名单（可重复，按 openid）；给了就切到名单模式",
    )
    parser.add_argument("--user", default="local", help="假通道里模拟的用户名（QQ 用不到）")
    parser.add_argument("--max-chars", type=int, default=4000, help="单条消息长度上限")
    parser.add_argument(
        "--approve", default="", metavar="配对码", help="批准一个配对码然后退出"
    )
    parser.add_argument(
        "--policy",
        choices=POLICIES,
        default=None,
        help="子进程的授权策略；不给则用 [channel.qqbot] 的 policy（再不然空=按已保存的设置）",
    )
    parser.add_argument(
        "--scope",
        default=None,
        help="允许自动落盘的范围；不给则用 [channel.qqbot] 的 scope（`--scope \"\"` 是不许落盘）",
    )
    parser.add_argument(
        "--session", default="", help="会话名。留空 = 用这条聊天的指向，没有就 bridge"
    )
    parser.add_argument(
        "--notify-disconnect",
        action="store_true",
        help="模型/外接服务从通变断时，给最近聊天发一句（默认关）",
    )
    _add_provider_args(parser, default="")
    # `default=None`：这三个的默认值来自 `[channel.qqbot]`，而 argparse 只要写了
    # 一个非 None 的默认值，解析完就成了一次"显式给的"——配置永远盖不过它。
    _add_web_args(parser, default=None)
    parser.add_argument(
        "--script",
        default="",
        help="仅测试回放：fake 的应答脚本 JSON。真实任务不要走这条",
    )
    parser.add_argument(
        "--allow-read",
        action="append",
        default=[],
        metavar="目录",
        help="授权额外可读目录（可重复，一次性，不写进配置）",
    )
    # `--root` 不给仍是**有意义的取值**：服务整台机器，落到哪由"这条聊天的
    # 指针"决定。所以默认空串 + optional_root，main() 不会拿 cwd 顶数。
    parser.set_defaults(func=qq_command, optional_root=True, channel="qqbot", root="")


def _add_diagnose_command(sub: argparse._SubParsersAction) -> None:
    """诊断通道的特权侧：由人（或人来跑的外部会话）执行。"""
    parser = sub.add_parser(
        "diagnose", help="处理 Agent 登记的环境/工具异常诊断请求"
    )
    parser.add_argument("--root", default=None, help="工作区路径（默认自动找）")
    parser.add_argument("--list", action="store_true", help="列出全部请求")
    parser.add_argument("--show", default="", help="看某条请求的完整内容")
    parser.add_argument("--report", default="", help="给某条请求写回报告")
    parser.add_argument("--verdict", default="", help="结论，一句话")
    parser.add_argument("--findings", default="", help="具体发现了什么")
    parser.add_argument("--evidence", default="", help="支撑结论的证据")
    parser.add_argument(
        "--init-key", action="store_true", help="生成签名密钥（放在项目外）"
    )
    parser.set_defaults(func=diagnose_command)


def _add_limits_command(sub: argparse._SubParsersAction) -> None:
    """能力标定值：看现在是多少、从哪来；按工作区覆盖。"""
    parser = sub.add_parser("limits", help="查看/覆盖能力标定值")
    parser.add_argument("--root", default=None, help="工作区路径（默认自动找）")
    parser.add_argument(
        "--set", action="append", default=[], metavar="名字=值",
        help="覆盖一项（写进 .agent/limits.json，可重复）",
    )
    parser.add_argument(
        "--reset", action="append", default=[], metavar="名字",
        help="恢复某项的默认值（可重复）",
    )
    parser.set_defaults(func=limits_command)


def _add_config_command(sub: argparse._SubParsersAction) -> None:
    """用户级默认配置：看现在用哪个 provider、从哪来。"""
    parser = sub.add_parser("config", help="查看/设置用户级默认配置")
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="键=值",
        help="设置一项（可重复），例如 --set provider=llamacpp",
    )
    parser.add_argument(
        "--reset", action="append", default=[], metavar="键", help="清除一项（可重复）"
    )
    parser.add_argument(
        "--path", dest="show_path", action="store_true", help="只打印配置文件路径"
    )
    parser.set_defaults(func=config_command)


def _add_chat_command(sub: argparse._SubParsersAction) -> None:
    """对话式使用：一行一句，共用同一个会话。"""
    parser = sub.add_parser("chat", help="对话式使用（多轮，共用同一个会话）")
    _add_provider_args(parser, default="")
    parser.add_argument(
        "--script",
        default="",
        help="仅测试回放：fake 的应答脚本 JSON。fake 不能写代码，真实任务不要走这条",
    )
    parser.add_argument("--window", type=int, default=0, help="0 表示自动向供应商查询")
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument("--step-ceiling", type=int, default=0)
    parser.add_argument("--no-supervise", action="store_true")
    parser.add_argument("--subagent-steps", type=int, default=12)
    parser.add_argument("--no-memory", action="store_true", help="关闭记忆读写")
    parser.add_argument(
        "--policy",
        choices=POLICIES,
        default="",
        help="本次会话的授权策略；留空则用已保存的设置",
    )
    parser.add_argument("--scope", default="", help="auto 策略下允许自动落盘的路径")
    parser.add_argument(
        "--session",
        default="",
        help="会话名。留空 = 用指针里那条，没有就用老默认名 chat",
    )
    _add_web_args(parser)
    parser.add_argument("--history", type=int, default=6)
    parser.add_argument(
        "--allow-read",
        action="append",
        default=[],
        metavar="目录",
        help="一次性额外目录（可重复，不写进配置）；持久关联用 spool extra add",
    )
    parser.set_defaults(func=chat_command)


def _add_mcp_command(sub: argparse._SubParsersAction) -> None:
    """把 agent 挂成 MCP 服务：外部 agent（Codex/Claude Code…）当用户下任务。"""
    parser = sub.add_parser(
        "mcp", help="以 MCP（stdio）服务的方式运行，供别的 agent 调用"
    )
    _add_provider_args(parser, default="")
    parser.add_argument(
        "--script",
        default="",
        help="仅测试回放：fake 的应答脚本 JSON。fake 不能写代码，真实任务不要走这条",
    )
    parser.add_argument(
        "--policy",
        choices=POLICIES,
        default="",
        help="子进程的授权策略；留空则用已保存的设置",
    )
    parser.add_argument(
        "--scope",
        default="",
        help="允许自动落盘的范围。外部 agent 的确认等于用户确认，边界靠它收窄",
    )
    parser.add_argument(
        "--session",
        default="",
        help="会话名。留空 = 用 mcp 的指向，没有就 mcp；单次调用还能用 session 覆盖",
    )
    _add_web_args(parser)
    parser.add_argument(
        "--allow-read",
        action="append",
        default=[],
        metavar="目录",
        help="授权额外可读目录（可重复）",
    )
    # `--root` 不给 = 服务整台机器（每次调用点名，或按 mcp 的指向）。
    parser.set_defaults(func=mcp_command, root="", optional_root=True)


def _add_mcp_servers_command(sub: argparse._SubParsersAction) -> None:
    """看/验用户配的外挂 MCP 服务。"""
    parser = sub.add_parser("mcp-servers", help="查看外挂 MCP 服务（--check 连一遍）")
    parser.add_argument("--check", action="store_true", help="真的连一遍，列出它们提供哪些工具")
    parser.set_defaults(func=mcp_servers_command)


def _add_bridge_command(sub: argparse._SubParsersAction) -> None:
    """把工作区接到一条聊天通道上（消息驱动的 agent）。"""
    parser = sub.add_parser(
        "bridge", help="把工作区接到聊天通道上（fake / telegram / wecom / qqbot）"
    )
    parser.add_argument(
        "--channel",
        default="fake",
        choices=("fake", "telegram", "wecom", "qqbot"),
        help="通道种类；fake 本地可跑，不需要任何凭据",
    )
    parser.add_argument(
        "--allow-user",
        action="append",
        default=[],
        metavar="用户",
        help="白名单：只有这些用户的消息会交给 agent（可重复）。给了就切到名单模式",
    )
    parser.add_argument(
        "--access",
        choices=("pairing", "allowlist", "open"),
        default="pairing",
        help=(
            "准入策略。pairing（默认）：陌生发送者拿配对码，由你用"
            " --approve 放行；allowlist：只放行 --allow-user 里的人；"
            "open：谁都放行（会警告）"
        ),
    )
    parser.add_argument(
        "--approve",
        default="",
        metavar="配对码",
        help="批准一个配对码然后退出（不用起通道）",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只看凭据与连通性：认到没有、从哪认到的、能不能连上（不起通道）",
    )
    parser.add_argument(
        "--user", default="local", help="假通道里模拟的用户名（配合白名单用）"
    )
    parser.add_argument(
        "--max-chars", type=int, default=4000, help="单条消息长度上限"
    )
    parser.add_argument(
        "--timeout", type=float, default=900.0, help="一轮任务最多等多少秒"
    )
    parser.add_argument(
        "--bridge-proxy",
        default="",
        metavar="地址",
        help=(
            "**通道自己**出网走哪个代理（如 socks5://127.0.0.1:1080）。"
            "本机 [[service]] 里名叫 tunnel 的配方通常就是这条 SOCKS"
            "（ssh -D）。QQ 平台有 IP 白名单：借一台云主机出去，"
            "白名单就固定成那台主机的 IP。注意这和 --proxy（模型供应商用）不是一回事"
        ),
    )
    parser.add_argument("--token", default="", help="Telegram bot token（或用环境变量）")
    parser.add_argument("--appid", default="", help="QQ 机器人的 AppID（或用环境变量）")
    parser.add_argument("--secret", default="", help="QQ 机器人的 AppSecret（或用环境变量）")
    parser.add_argument(
        "--sandbox", action="store_true", help="QQ 机器人用沙箱环境（sandbox.api.sgroup.qq.com）"
    )
    _add_provider_args(parser, default="")
    _add_web_args(parser)
    parser.add_argument(
        "--script",
        default="",
        help="仅测试回放：fake 的应答脚本 JSON。fake 不能写代码，真实任务不要走这条",
    )
    parser.add_argument(
        "--policy", choices=POLICIES, default="", help="子进程的授权策略"
    )
    parser.add_argument(
        "--scope", default="", help="允许自动落盘的范围（越界仍会退回确认）"
    )
    parser.add_argument("--session", default="bridge", help="会话名")
    parser.add_argument(
        "--notify-disconnect",
        action="store_true",
        help="模型/外接服务从通变断时，给最近聊天发一句（默认关，避免刷屏）",
    )
    # `--root` 不给是**有意义的取值**：服务整台机器，具体落到哪由"这条聊天的
    # 指针"决定。所以这里既把默认值定成空串，也打上 optional_root 让 main()
    # 别替它自动找一个工作区。
    parser.set_defaults(func=bridge_command, root="", optional_root=True)


def _add_extra_command(sub: argparse._SubParsersAction) -> None:
    """本机关联工作区外的目录。聊天里加不了。"""
    parser = sub.add_parser(
        "extra",
        help="关联工作区外的目录（本机指令；读放开，写要确认；聊天里加不了）",
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=["add", "remove", "list"],
        help="add 写入配置，remove 删掉，list 查看（默认）",
    )
    parser.add_argument("path", nargs="?", default="", help="要关联或取消的目录")
    parser.add_argument("--root", default=None, help="工作区路径（默认自动找）")
    parser.set_defaults(func=extra_command)


def _add_service_command(sub: argparse._SubParsersAction) -> None:
    """本机外接配方：快捷拉起 / 停掉。hub 不替你启 llama / 隧道。"""
    parser = sub.add_parser(
        "service",
        help="本机外接配方（llama 模型进程、tunnel 通道 SOCKS 等：start / stop / list）",
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=["list", "start", "stop"],
        help="list 查看，start 拉起，stop 停掉（默认 list）",
    )
    parser.add_argument(
        "name",
        nargs="?",
        default="",
        help="配方名；start/stop 不给则对全部（start 只动有 command 的）",
    )
    parser.set_defaults(func=service_command)


def _add_status_command(sub: argparse._SubParsersAction) -> None:
    """一眼看清这台机器上的 spoolkit 服务，以及怎么起 / 怎么停。

    为什么值得一条顶层命令：用户报的是"**缺乏全局命令能够观察/管理服务状态**"
    ——模型在哪、网页壳起没起、通道凭据认到没有、密钥落在哪，原先散在
    `spool service` / `spool bridge --check` / `spool env` 三个地方，而且没有
    一条命令回答"现在到底有没有东西在跑"。这里给一张表，最后一列是怎么起停。

    **只打本机地址、绝不联网**；探活超时短（默认 0.5 秒，探的是回环地址），
    失败不冒栈，退出码：0 = 关键都在，1 = 有关键没起，2 = 用法错 / 配置读不出来。
    """
    parser = sub.add_parser(
        "status",
        help="一眼看清本机服务（模型 / 网页壳 / 通道 / MCP / 工作区 / 密钥落点）与怎么起停",
        description=(
            "把这台机器上 spoolkit 相关的东西摆成一张表：每行是服务、状态、地址或说明，"
            "以及**怎么起 / 怎么停**。只打本机地址（127.0.0.1），不联网；"
            "每条探测点都是短超时，失败给一句人话 + 非零退出码。"
            "关键项（没起 → 退出码 1）：模型供应商没配 / 生效值是 fake、"
            "生效 provider 是 llamacpp 时的本机模型、用户配置 [[service]] 里写了 command 的配方。"
        ),
    )
    parser.add_argument(
        "--json", action="store_true", help="以 JSON 输出（字段名稳定，给脚本用）"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=status_report.DEFAULT_TIMEOUT,
        help=(
            "每个探测点的超时秒数（默认 0.5：探的都是回环地址，"
            "正常监听时毫秒级就有回音；等模型加载完可以给大一点）"
        ),
    )
    parser.add_argument(
        "--web-port",
        action="append",
        type=int,
        default=[],
        metavar="端口",
        help="除默认 8765 外，还要看哪个端口上的网页壳（可重复）",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="真连一次外挂 MCP（默认只列出登记了哪些；真连会拉起它们的子进程）",
    )
    parser.add_argument(
        "--mcp-timeout",
        type=float,
        default=status_report.DEFAULT_MCP_TIMEOUT,
        help="`--probe` 连外挂 MCP 的超时秒数（默认 5：要起子进程，0.5 秒不够）",
    )
    parser.set_defaults(func=status_command)


def _add_help_command(sub: argparse._SubParsersAction) -> None:
    """`spool help [子命令]`：等价于 `spool --help` / `spool <子命令> --help`。

    用户敲过 `spool help`，当时得到的是一句 `invalid choice: 'help'`。这条命令
    只是把它转发成一次带 `--help` 的重新解析（见 `commands/help.py`），
    帮助文本与 `spool <子命令> --help` 逐字相同。
    """
    parser = sub.add_parser(
        "help", help="看帮助（= `spool --help`）：spool help [子命令]"
    )
    parser.add_argument(
        "topic", nargs="?", default="", metavar="子命令", help="留空看总帮助"
    )
    parser.set_defaults(func=help_command)


def _add_init_command(sub: argparse._SubParsersAction) -> None:
    """把一个目录做成工作区（幂等）。"""
    parser = sub.add_parser(
        "init", help="把当前目录（或给定路径）做成工作区：建 .agent/ 并登记"
    )
    # 位置参数而不是 `--root`：init 的对象是"哪个目录"，而且它**绝不能**参与
    # main() 里那套"自动找已有工作区"的解析——否则在别处敲 `spool init`，
    # 它会一路找到登记表里的旧工作区去，而不是初始化你脚下这个目录。
    parser.add_argument("path", nargs="?", default=None, help="目录，默认当前目录")
    parser.set_defaults(func=init_command)


def _add_approve_command(sub: argparse._SubParsersAction) -> None:
    """批准聊天通道的配对码。手机配对时最需要随手敲的动作，所以放在顶层。"""
    parser = sub.add_parser(
        "approve", help="批准一个配对码（等价于 bridge --approve）"
    )
    parser.add_argument("code", metavar="配对码", nargs="?", default="")
    parser.add_argument("--root", default=None, help="工作区路径（默认自动找）")
    parser.add_argument("--list", action="store_true", help="看放行了谁、还有谁在等")
    parser.add_argument(
        "--revoke", default="", metavar="用户", help="撤销某个人的放行（解绑）"
    )
    parser.add_argument(
        "--forget", default="", metavar="配对码", help="丢掉一个还没批准的码"
    )
    parser.add_argument(
        "--ws",
        default="",
        metavar="名字",
        help=(
            "批准时点名工作区（旧配对文件里的码没有工作区维度；"
            "登记表里有多个工作区时**必须**点名）"
        ),
    )
    # `--root` 不给是正常的：机器级的码自带工作区，旧式的码按
    # "当前目录 → 登记表" 退。所以别在这里替它自动找一个工作区。
    parser.set_defaults(func=approve_entry, optional_root=True)


def configure_stdio() -> None:
    """把标准输出/错误固定成 UTF-8，且**永不因为一个字符崩掉整个运行**。

    为什么必须有：Windows 上默认编码是 cp936，而输出一旦被重定向或走管道
    （`spool run … > log.txt`、任何子进程捕获、Web 壳），Python 就按
    locale 编码写字节——进度块里的 `✓` 编不出来，于是
    `UnicodeEncodeError: 'gbk' codec can't encode character '\\u2713'`
    直接把整个运行打断。它已经在一次 50 题的长跑里真发生过。

    控制台直连时 Python 走的是控制台 API，所以这个坑只在重定向/管道里现身——
    而脚本化、录日志、Web 壳全都走管道。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - 少数被包装过的流
            pass


def _start_here(parser: argparse.ArgumentParser) -> int:
    """什么都不带就敲 `spool` 时该发生什么。

    想学的是 Claude Code 那样"在任何目录敲一下就能起手"：不是工作区就问一句
    要不要在这里初始化，是工作区就直接进对话。

    非交互环境（管道、CI、被别的程序调起）**不弹问题**：那会挂住别人的自动化。
    那种情况下打帮助并返回非零，让人显式说出他想干什么。
    """
    if not workspace.find_root() and not workspace.known():
        if not onboarding.interactive():
            parser.print_help()
            return 2
        answer = input("当前目录还不是工作区，在这里初始化吗？[Y/n] ").strip().lower()
        if answer not in ("", "y", "yes"):
            parser.print_help()
            return 1
        here = Path.cwd().resolve()
        (here / workspace.MARK).mkdir(parents=True, exist_ok=True)
        workspace.register(here)
        print(f"已初始化工作区：{here}")
    return main(["chat"])


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(prog="spool")
    parser.add_argument(
        # 两个名字都报出来：命令叫 `spool`，装的那个包叫 `spoolkit`，
        # 对不上时人才知道该 `pip uninstall` 谁。
        "--version", action="version", version=f"spool {__version__} (spoolkit)"
    )
    sub = parser.add_subparsers(dest="command", required=False)
    _add_init_command(sub)
    _add_ws_command(sub)
    _add_run_command(sub)
    _add_plan_command(sub)
    _add_policy_command(sub)
    _add_revert_command(sub)
    _add_bench_command(sub)
    _add_session_command(sub)
    _add_serve_command(sub)
    _add_hub_command(sub)
    _add_web_command(sub)
    _add_qq_command(sub)
    _add_diagnose_command(sub)
    _add_limits_command(sub)
    _add_config_command(sub)
    _add_env_command(sub)
    _add_chat_command(sub)
    _add_mcp_command(sub)
    _add_mcp_servers_command(sub)
    _add_bridge_command(sub)
    _add_approve_command(sub)
    _add_extra_command(sub)
    _add_service_command(sub)
    _add_status_command(sub)
    _add_help_command(sub)

    args = parser.parse_args(argv)
    if args.command is None:
        return _start_here(parser)
    # 工作区只在这里解析一次（规则见 workspace.py）。放在这里而不是各条子命令里，
    # 是因为十几个 `Path(args.root).resolve()` 迟早会分叉——而分叉的症状是
    # "某个命令的记忆跟别的命令不是同一份"，那种问题最难查。
    if hasattr(args, "root"):
        try:
            if getattr(args, "optional_root", False):
                # `bridge` / `serve` / `hub`：**不给 `--root` 是一个有意义的取值**
                # （服务整台机器 / 由指针决定），所以不能在这里替它自动找一个
                # 工作区。给了才解析。
                args.root = (
                    str(workspace.resolve_root_interactive(args.root))
                    if str(getattr(args, "root", "") or "").strip()
                    else ""
                )
            else:
                # 走 `resolve_root_interactive`：登记表里有多个工作区、而当前目录不是
                # 工作区时，终端里要能问一句（内核自己不认识终端，chooser 由 CLI 注入）。
                args.root = str(workspace.resolve_root_interactive(args.root))
        except workspace.WorkspaceError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if hasattr(args, "session"):
        # `--session` 留空表示"交给内核定"（显式 > 指针 > 该入口的老默认名）。
        # 各命令的 argparse 默认值必须是空串：只要默认值还在，它就永远是"显式给的"，
        # 指针永远轮不到——这条正是写计划时才发现的那个坑。
        args.session = str(getattr(args, "session", "") or "")
    # 第一次用（没配过供应商）且人在终端前：先向导，再干活。
    # 非交互环境绝不弹问题——那会挂住别人的脚本；那种场景由 provider_gateway
    # 给一句明确的指路。
    if (
        hasattr(args, "provider")
        and not getattr(args, "no_setup", False)
        and onboarding.needs_setup(argv)
        and onboarding.interactive()
    ):
        # 先交代"找过哪儿"：不然人只会看到"又问了我一遍"。
        print(settings.describe_missing())
        # 向导要往 `.env` 写模型密钥，所以**它必须知道写到哪个工作区**。
        # `optional_root` 的入口（web / hub / bridge / mcp）没给 `--root` 时
        # `args.root` 是空串，而 `Path("")` 恰好是 cwd——照那样写，密钥会落进
        # "你碰巧站在哪个目录"，而向导还会这么说。机器级入口没有工作区就
        # **不给工作区**：密钥写用户级那份 `.env`（那才是"这台机器上的你"
        # 该放密钥的地方，见 settings.env_path）。
        wizard_root = str(getattr(args, "root", "") or "").strip()
        onboarding.run_wizard(Path(wizard_root).resolve() if wizard_root else None)
    # 供应商相关的取值统一在这里落地一次（命令行 > 环境变量 > 用户配置），
    # 好让每条子命令看到的都是**生效值**——否则 bench 自己拼网关、
    # serve 转发参数、记忆里记的模型名，各拿各的默认，症状是"我设了但没用"。
    if hasattr(args, "provider"):
        for key, value in resolve_provider_args(args).items():
            setattr(args, key, value)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
