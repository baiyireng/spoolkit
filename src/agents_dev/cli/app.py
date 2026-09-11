"""命令行入口。

这里只做两件事：定义参数、把命令分发到对应实现。
装配细节在 runtime.py，选项决策在 options.py，各条命令在 commands/ 下。

保持入口文件薄是有原因的：它曾经有近 800 行，把参数解析、装配、
三条执行路径全混在一起，结果一个闭包变量绑定错误藏了整整一轮才被发现——
文件太长时，读不完就看不出来。
"""

import argparse

from agents_dev.bench import DEFAULT_BENCH_ROOT
from agents_dev.cli.commands.bench import bench
from agents_dev.cli.commands.plan import make_plan
from agents_dev.cli.commands.policy import policy_command
from agents_dev.cli.commands.revert import revert_command
from agents_dev.cli.commands.run import run
from agents_dev.cli.options import (
    DEFAULT_WINDOW,
    MAX_AUTO_WINDOW,
    report_policy,
    resolve_policy,
    resolve_scope,
    resolve_window,
)
from agents_dev.cli.runtime import (
    PREFETCH_BUDGET,
    assemble_loop,
    build_approver,
    build_lessons,
    build_loop,
    build_memory,
    open_memory,
    settle_lessons,
    show_history,
)
from agents_dev.llm.providers import describe_providers, provider_names
from agents_dev.policy import POLICIES

# 这些名字以前直接定义在 app.py 里，外部（含测试）按此路径导入。
# 拆文件之后在这里重新导出，避免留下断裂的导入路径。
__all__ = [
    "DEFAULT_WINDOW",
    "MAX_AUTO_WINDOW",
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
    parser.add_argument(
        "--provider",
        "--engine",
        dest="provider",
        choices=provider_names(),
        default=default,
        help="模型供应商；" + describe_providers(),
    )
    parser.add_argument("--model", default="", help="留空则用供应商默认模型")
    parser.add_argument("--base-url", default="", help="llama.cpp 服务地址")
    parser.add_argument("--proxy", default="", help="留空则使用系统代理")
    parser.add_argument("--root", default=".")


def _add_run_command(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("run", help="运行一次任务")
    parser.add_argument("--goal", default="")
    _add_provider_args(parser, default="fake")
    parser.add_argument("--script", default="", help="假模型脚本 JSON")
    parser.add_argument("--window", type=int, default=0, help="0 表示自动向供应商查询")
    parser.add_argument("--max-steps", type=int, default=10)
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
        "--session", default="cli", help="会话名。不同时段/目的的活分开记"
    )
    parser.add_argument(
        "--history", type=int, default=6, help="启动时显示最近几轮会话记录"
    )
    parser.add_argument("--no-history", action="store_true", help="不显示会话记录")
    parser.set_defaults(func=run)


def _add_plan_command(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("plan", help="把目标拆成可验收的步骤序列")
    parser.add_argument("--goal", required=True)
    _add_provider_args(parser, default="gemini")
    parser.add_argument("--limit", type=int, default=10)
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
    parser.add_argument("--window", type=int, default=0)
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument(
        "--verbose", action="store_true", help="打印每个任务的轨迹与验收输出"
    )
    parser.set_defaults(func=bench)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agents-dev")
    sub = parser.add_subparsers(dest="command", required=True)
    _add_run_command(sub)
    _add_plan_command(sub)
    _add_policy_command(sub)
    _add_revert_command(sub)
    _add_bench_command(sub)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

