"""`agents-dev bridge`：把工作区接到一条聊天通道上。

三种通道：

- `fake`：本地可跑（stdin 一行 = 一条消息，回复打到 stdout）。**今天就能用**，
  也是验这套东西的唯一不依赖外部服务的方式；
- `telegram`：需要 `AGENTS_DEV_TELEGRAM_TOKEN`；长轮询，不需要公网入口；
- `wecom`：企业微信自建应用，需要 corp_id/secret/agent_id；发消息本机可用，
  **收**消息要一个公网回调 URL（文档里写了形状）。

白名单：`--allow-user` 可以给多次。**不给就等于谁都能驱动这个工作区**——
所以交互式启动时会明说这一点。
"""

import argparse
import sys
import time
from pathlib import Path

from agents_dev.bridge.agent_runner import AgentRunner
from agents_dev.bridge.core import Bridge
from agents_dev.bridge.fake import FakeChannel


def _build_channel(args, log) -> object:
    if args.channel == "fake":
        return FakeChannel()
    if args.channel == "telegram":
        from agents_dev.bridge.telegram import TelegramChannel

        token = args.token or __import__("os").environ.get("AGENTS_DEV_TELEGRAM_TOKEN", "")
        if not token:
            raise SystemExit(
                "Telegram 需要 token：设 AGENTS_DEV_TELEGRAM_TOKEN，或用 --token 给。"
                "（怎么建机器人见 docs/bridge.md）"
            )
        return TelegramChannel(token, proxy=args.proxy or None)
    if args.channel == "wecom":
        from agents_dev.bridge.wecom import WeComChannel

        channel = WeComChannel()
        if not (channel.corp_id and channel.secret and channel.agent_id):
            raise SystemExit(
                "企业微信需要三个值：AGENTS_DEV_WECOM_CORP_ID / _SECRET / _AGENT_ID"
                "（见 docs/bridge.md）"
            )
        log("企业微信通道：发消息本机可用；**收**消息需要公网回调 URL（见文档）")
        return channel
    raise SystemExit(f"没有这种通道：{args.channel}")


def _fake_loop(bridge: Bridge, channel: FakeChannel, user: str) -> int:
    """本地 loop：stdin 一行一条消息，回复打到 stdout。

    它是"今天就能用"的那条路，也是不给外部凭据时唯一能验完整链路的方式。
    """
    print("假通道已就绪：一行一条消息，回车发送；/exit 退出。")
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        if text in ("/exit", "/quit"):
            break
        channel.push(user, text)
        for reply in bridge.run_once():
            print(f"→ {reply.text}")
    return 0


def bridge_command(args: argparse.Namespace) -> int:
    project_root = Path(args.root).resolve()
    allowed = {str(item) for item in (getattr(args, "allow_user", []) or []) if str(item).strip()}
    if not allowed:
        print(
            "⚠ 没给 --allow-user：**任何人都能驱动这个工作区**。"
            "建议至少给一个（Telegram 用数字 id、企业微信用 userid）。",
            file=sys.stderr,
        )

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

    runner = AgentRunner(
        project_root,
        session=args.session,
        extra_args=extra,
        timeout=args.timeout,
    )
    channel = _build_channel(args, lambda text: print(text, file=sys.stderr))
    bridge = Bridge(
        channel,
        runner,
        allowed_users=allowed,
        max_chars=args.max_chars,
        on_note=lambda text: print(f"· {text}", file=sys.stderr),
    )

    if args.channel == "fake":
        return _fake_loop(bridge, channel, args.user)

    print(f"{args.channel} 通道已就绪，等消息（Ctrl-C 退出）。", file=sys.stderr)
    try:
        while True:
            replies = bridge.run_once()
            for reply in replies:
                if reply.accepted:
                    print(f"· 处理了 {reply.user} 的一条消息", file=sys.stderr)
            if not replies:
                # 长轮询通道自己会等；这里只是别把 CPU 打满。
                time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n收到中断，退出。", file=sys.stderr)
        return 0
    finally:
        channel.close()
