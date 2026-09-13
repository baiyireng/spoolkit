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
from agents_dev.bridge.pairing import ALLOWLIST, OPEN, PAIRING, POLICIES, Pairings
from agents_dev.bridge.plugins import load_channel_factories


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
    if args.channel == "qqbot":
        from agents_dev.bridge.qqbot import QQBotChannel

        app_id = args.appid or __import__("os").environ.get("AGENTS_DEV_QQ_APPID", "")
        secret = args.secret or __import__("os").environ.get("AGENTS_DEV_QQ_SECRET", "")
        if not (app_id and secret):
            raise SystemExit(
                "QQ 官方机器人需要 AppID 与 AppSecret："
                "设 AGENTS_DEV_QQ_APPID / AGENTS_DEV_QQ_SECRET，或用 --appid / --secret 给。"
                "（在 QQ 机器人开放平台建应用后能看到；沙箱加 --sandbox）"
            )
        channel = QQBotChannel(
            app_id=app_id,
            secret=secret,
            sandbox=args.sandbox,
            transport=None,
        )
        # 收事件是长连接，起后台线程；桥那边只是 poll。
        channel.start()
        log("QQ 机器人通道已起：网关长连接在后台，消息进来就交给 agent。")
        return channel
    # 内置的三种都不匹配：去装载的通道插件里找。
    # 这样接一家新通道（比如第三方 hook 的个人微信/QQ）不必改这个文件——
    # 插件自己注册，核心不认识它。
    factories = load_channel_factories()
    factory = factories.get(args.channel)
    if factory is not None:
        return factory(
            {
                "name": args.channel,
                "root": str(args.root),
                "token": getattr(args, "token", ""),
                "proxy": getattr(args, "proxy", ""),
            }
        )
    if factories:
        log(f"装载过的通道插件：{'、'.join(sorted(factories))}")
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
    pairings = Pairings(project_root / ".agent" / "bridge-pairings.json")

    if args.approve:
        user = pairings.approve_code(args.approve)
        if not user:
            print(f"没有这个配对码：{args.approve}", file=sys.stderr)
            return 2
        print(f"已批准 {user}（写进 {pairings.path}）")
        return 0

    allowed = {str(item) for item in (getattr(args, "allow_user", []) or []) if str(item).strip()}
    access = args.access
    if allowed and access == PAIRING:
        # `--allow-user` 是"这几个人直接放行"，语义上属于名单模式。
        access = ALLOWLIST
    if access == OPEN:
        print(
            "⚠ access=open：**任何人都能驱动这个工作区**（你显式选了这一档）。",
            file=sys.stderr,
        )
    elif not allowed:
        print(
            "按配对模式运行：陌生发送者会拿到一个配对码，"
            "你用 `agents-dev bridge --approve <码>` 放行。",
            file=sys.stderr,
        )
    if pairings.approved:
        print(f"已批准：{'、'.join(pairings.approved)}", file=sys.stderr)
    if pairings.pending:
        print(
            "待配对：" + "、".join(f"{user}（码 {code}）" for code, user in pairings.pending),
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
        pairings=pairings,
        access=access,
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
