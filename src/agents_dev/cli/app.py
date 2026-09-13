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

from agents_dev import __version__
from agents_dev import onboarding
from agents_dev.bench import DEFAULT_BENCH_ROOT
from agents_dev.cli.commands.bench import bench
from agents_dev.cli.commands.plan import make_plan
from agents_dev.cli.commands.policy import policy_command
from agents_dev.cli.commands.revert import revert_command
from agents_dev.cli.commands.run import run
from agents_dev.cli.commands.serve import serve_command
from agents_dev.cli.commands.diagnose import diagnose_command
from agents_dev.cli.commands.session import session_command
from agents_dev.cli.commands.limits import limits_command
from agents_dev.cli.commands.config import config_command
from agents_dev.cli.commands.chat import chat_command
from agents_dev.cli.commands.mcp import mcp_command
from agents_dev.cli.options import (
    DEFAULT_WINDOW,
    report_policy,
    resolve_policy,
    resolve_scope,
    resolve_window,
)
from agents_dev.cli.runtime import (
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
from agents_dev.llm.providers import describe_providers, provider_names
from agents_dev.policy import POLICIES

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
    用户在 `agents-dev config` 里设的默认永远盖不过它。
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
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--no-setup",
        action="store_true",
        help="第一次运行时不要弹配置向导（脚本/CI 里用）",
    )


def _add_run_command(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("run", help="运行一次任务")
    parser.add_argument("--goal", default="")
    _add_provider_args(parser, default="fake")
    parser.add_argument("--script", default="", help="假模型脚本 JSON")
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
        "--cover",
        default="",
        help=(
            "拆解后要检查覆盖的清单（逗号分隔）。目标明说「把这 N 件事都做完」"
            "时才给；不给就不检查——什么算「必须覆盖」是关于目标的判断，"
            "不该由 harness 猜"
        ),
    )
    parser.add_argument(
        "--session", default="cli", help="会话名。不同时段/目的的活分开记"
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
        "--allow-read",
        action="append",
        default=[],
        metavar="目录",
        help=(
            "授权额外可读目录（可重复）。只放开读，写入仍限工作区内；"
            "工作区绑定不变"
        ),
    )
    parser.set_defaults(func=run)


def _add_plan_command(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("plan", help="把目标拆成可验收的步骤序列")
    parser.add_argument("--goal", required=True)
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
    parser.add_argument("--root", default=".")
    parser.set_defaults(func=policy_command)


def _add_revert_command(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("revert", help="回滚上一次写入的改动")
    parser.add_argument("--root", default=".")
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
    parser = sub.add_parser("session", help="列出这个工作区里的会话")
    parser.add_argument("--root", default=".")
    parser.set_defaults(func=session_command)


def _add_serve_command(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("serve", help="启动 Web UI 壳")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--session", default="cli")
    parser.add_argument("--provider", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--script", default="", help="假模型的应答脚本（配合 --provider fake）")
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
    parser.add_argument("--root", default=".")
    parser.set_defaults(func=serve_command)


def _add_diagnose_command(sub: argparse._SubParsersAction) -> None:
    """诊断通道的特权侧：由人（或人来跑的外部会话）执行。"""
    parser = sub.add_parser(
        "diagnose", help="处理 Agent 登记的环境/工具异常诊断请求"
    )
    parser.add_argument("--root", default=".")
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
    parser.add_argument("--root", default=".")
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
    parser.add_argument("--script", default="", help="假模型脚本 JSON")
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
    parser.add_argument("--session", default="chat", help="会话名。不同时段/目的的活分开记")
    parser.add_argument("--history", type=int, default=6)
    parser.add_argument(
        "--allow-read",
        action="append",
        default=[],
        metavar="目录",
        help="授权额外可读目录（可重复）。只放开读，写入仍限工作区内",
    )
    parser.set_defaults(func=chat_command)


def _add_mcp_command(sub: argparse._SubParsersAction) -> None:
    """把 agent 挂成 MCP 服务：外部 agent（Codex/Claude Code…）当用户下任务。"""
    parser = sub.add_parser(
        "mcp", help="以 MCP（stdio）服务的方式运行，供别的 agent 调用"
    )
    _add_provider_args(parser, default="")
    parser.add_argument("--script", default="", help="假模型脚本 JSON")
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
    parser.add_argument("--session", default="mcp", help="会话名")
    parser.add_argument(
        "--allow-read",
        action="append",
        default=[],
        metavar="目录",
        help="授权额外可读目录（可重复）",
    )
    parser.set_defaults(func=mcp_command)


def configure_stdio() -> None:
    """把标准输出/错误固定成 UTF-8，且**永不因为一个字符崩掉整个运行**。

    为什么必须有：Windows 上默认编码是 cp936，而输出一旦被重定向或走管道
    （`agents-dev run … > log.txt`、任何子进程捕获、Web 壳），Python 就按
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


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(prog="agents-dev")
    parser.add_argument(
        "--version", action="version", version=f"agents-dev {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    _add_run_command(sub)
    _add_plan_command(sub)
    _add_policy_command(sub)
    _add_revert_command(sub)
    _add_bench_command(sub)
    _add_session_command(sub)
    _add_serve_command(sub)
    _add_diagnose_command(sub)
    _add_limits_command(sub)
    _add_config_command(sub)
    _add_chat_command(sub)
    _add_mcp_command(sub)

    args = parser.parse_args(argv)
    # 第一次用（没配过供应商）且人在终端前：先向导，再干活。
    # 非交互环境绝不弹问题——那会挂住别人的脚本；那种场景由 provider_gateway
    # 给一句明确的指路。
    if (
        hasattr(args, "provider")
        and not getattr(args, "no_setup", False)
        and onboarding.needs_setup(argv)
        and onboarding.interactive()
    ):
        onboarding.run_wizard(Path(getattr(args, "root", ".")).resolve())
    # 供应商相关的取值统一在这里落地一次（命令行 > 环境变量 > 用户配置），
    # 好让每条子命令看到的都是**生效值**——否则 bench 自己拼网关、
    # serve 转发参数、记忆里记的模型名，各拿各的默认，症状是"我设了但没用"。
    if hasattr(args, "provider"):
        for key, value in resolve_provider_args(args).items():
            setattr(args, key, value)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
