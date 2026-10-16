"""启动 Web UI 壳。"""

import argparse
from pathlib import Path

from spoolkit.kernel.target import resolve_target
from spoolkit.web.server import serve


def _web_flags(args: argparse.Namespace) -> list[str]:
    """`--web` / `--no-web`。没写 `web` 当开；旧的 `no_web` 仍认。"""
    if getattr(args, "web", True) and not getattr(args, "no_web", False):
        return ["--web"]
    return ["--no-web"]


def extra_from_serve_args(args: argparse.Namespace) -> list[str]:
    """网页壳转发给 `run` 子进程的那组参数。hub 与 serve 共用。"""
    extra: list[str] = []
    for flag, value in (
        ("--provider", args.provider),
        ("--model", args.model),
        ("--base-url", args.base_url),
        ("--script", args.script),
        ("--proxy", args.proxy),
        ("--policy", args.policy),
        ("--scope", args.scope),
    ):
        if value:
            extra += [flag, value]
    for root in getattr(args, "allow_read", []) or []:
        extra += ["--allow-read", root]
    # QQ / 网页 / CLI 同一套：默认开联网；关掉才转 --no-web。
    extra += _web_flags(args)
    api = str(getattr(args, "web_search_api", "") or "").strip()
    if api:
        extra += ["--web-search-api", api]
    for domain in getattr(args, "web_allow", []) or []:
        extra += ["--web-allow", domain]
    for domain in getattr(args, "web_deny", []) or []:
        extra += ["--web-deny", domain]
    # 步数预算：MCP 派发长任务时要能一次给足——不给的话默认 10 步会把复杂任务
    # 逼成「靠督导 +3/+5 续期」零敲碎打。`serve`/`hub` 的同名默认值与 `run` 一致，
    # 转发不改变它们的行为。
    max_steps = getattr(args, "max_steps", None)
    if max_steps:
        extra += ["--max-steps", str(max_steps)]
    return extra


def serve_command(args: argparse.Namespace) -> int:
    """把命令行 agent 包在本地 HTTP 服务里。

    服务自己起子进程跑 CLI，所以这里的那些参数是**转发**给子进程的：
    换模型、换代理、换授权策略都在这里指定，不必再去改子进程的命令行。

    `--script` 也转发：网页壳的确认流程要用假模型**带脚本**本地走一遍。
    fake 不能生成代码；没有脚本时子进程会立刻退出。

    目标有两种口径：

    - **给了 `--root`**：钉住这一个工作区（老行为逐字保留：脚本、`.bat` 都靠它）。
      会话仍走内核解析（client=`web`，兜底 `cli`），并**把解析结果显式传给
      子进程**——不然子进程会再按自己的入口规则解析一次，出现"页面显示一条、
      实际跑的是另一条"；
    - **没给 `--root`**：机器级。**这里不解析任何目标**（设计 §3.2：机器级入口
      不认 cwd，"我碰巧站在哪个目录"不该决定这个服务服务谁）。初始目标由内核
      顺位在服务里定，定不出来就先空着——页面来选。
    """
    explicit = str(getattr(args, "root", "") or "").strip()
    if not explicit:
        serve(
            None,
            host=args.host,
            port=args.port,
            session=str(getattr(args, "session", "") or ""),
            extra_args=extra_from_serve_args(args),
            pinned=False,
        )
        return 0
    target = resolve_target(
        workspace=args.root,
        session=args.session,
        client="web",
        session_fallback="cli",
        # 显式给了工作区时这一层本来就用不上（显式值优先）；写出来是为了把
        # "这条入口不认 cwd"钉在代码里，而不是靠调用顺序碰巧成立。
        use_cwd=False,
    )
    serve(
        target.root,
        host=args.host,
        port=args.port,
        session=target.session,
        extra_args=extra_from_serve_args(args),
        # `target` 一起给过去：`/state` 要交代"这条目标是谁定的"（显式 / 指针 /
        # 登记表），让服务自己再解析一遍会把"来自指针"记成"显式给的"。
        pinned=True,
        target=target,
    )
    return 0
