"""`spool bridge`：把工作区接到一条聊天通道上。

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

from spoolkit.bridge.agent_runner import AgentRunner
from spoolkit.bridge.core import Bridge
from spoolkit.bridge.fake import FakeChannel
from spoolkit.bridge.pairing import ALLOWLIST, OPEN, PAIRING, POLICIES, Pairings
from spoolkit.bridge.plugins import load_channel_factories


def _build_channel(args, log) -> object:
    root = Path(getattr(args, "root", ".")).resolve()
    if args.channel == "fake":
        return FakeChannel()
    if args.channel == "telegram":
        from spoolkit.bridge.telegram import TelegramChannel

        channel = TelegramChannel(args.token, root=root, proxy=args.proxy or None)
        if not channel.token:
            raise SystemExit(
                "Telegram 需要 token。三种给法：\n"
                f"  1) 写进 {root}\\.env：AGENTS_DEV_TELEGRAM_TOKEN=...\n"
                "  2) 设环境变量 AGENTS_DEV_TELEGRAM_TOKEN\n"
                "  3) 命令行 --token。怎么建机器人见 docs/bridge.md"
            )
        return channel
    if args.channel == "wecom":
        from spoolkit.bridge.wecom import WeComChannel

        channel = WeComChannel(root=root)
        if not (channel.corp_id and channel.secret and channel.agent_id):
            raise SystemExit(
                "企业微信需要三个值，写进 "
                f"{root}\\.env 或者设成环境变量：\n"
                "  AGENTS_DEV_WECOM_CORP_ID / AGENTS_DEV_WECOM_SECRET / "
                "AGENTS_DEV_WECOM_AGENT_ID\n（怎么建自建应用见 docs/bridge.md）"
            )
        log("企业微信通道：发消息本机可用；**收**消息需要公网回调 URL（见文档）")
        return channel
    if args.channel == "qqbot":
        from spoolkit.bridge.qqbot import QQBotChannel

        # 凭据的解析放在通道里做（它认 `.env`，也认几种常见拼写），
        # 这里只负责"没有就给一句能照着做的话"。
        channel = QQBotChannel(
            app_id=args.appid,
            secret=args.secret,
            sandbox=args.sandbox,
            root=root,
            proxy=args.bridge_proxy,
            transport=None,
            on_note=log,
        )
        if not (channel.app_id and channel.secret):
            raise SystemExit(
                "QQ 官方机器人需要 AppID 与 AppSecret。三种给法：\n"
                f"  1) 写进 {root}\\.env（推荐，已被 .gitignore 排除）：\n"
                "       QQ_AppID=102xxxxxx\n"
                "       QQ_AppSecret=xxxxxxxx\n"
                "  2) 设环境变量 AGENTS_DEV_QQ_APPID / AGENTS_DEV_QQ_SECRET\n"
                "  3) 命令行 --appid / --secret\n"
                "（在 QQ 机器人开放平台建应用后能看到；沙箱加 --sandbox）"
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

    if getattr(args, "check", False):
        return _check(project_root, args)

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
            "你用 `spool bridge --approve <码>` 放行。",
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


def _check(project_root: Path, args) -> int:
    """`--check`：只看凭据与连通性，**不起通道、不跑 agent**。

    初次配置最需要的就这一句：**它到底认到我的凭据没有、从哪认到的**。
    没有它，配错了只能靠"机器人不回话"来猜。
    """
    from spoolkit.bridge import credentials

    print(f"工作区：{project_root}")
    print(f"凭据文件：{project_root / '.env'}"
          + ("" if (project_root / ".env").is_file() else "（不存在）"))
    channel = args.channel
    if channel == "qqbot":
        app_id, id_source = credentials.find(project_root, *credentials.QQ_APP_ID)
        secret, secret_source = credentials.find(project_root, *credentials.QQ_SECRET)
        print(f"AppID：{credentials.mask(app_id)}（{id_source}）")
        print(f"AppSecret：{credentials.mask(secret)}（{secret_source}）")
        if not (app_id and secret):
            print("\n缺凭据。写进 .env 就行：QQ_AppID=… 与 QQ_AppSecret=…")
            return 2
        from spoolkit.bridge.qqbot import QQBotChannel

        qq = QQBotChannel(
            app_id=app_id, secret=secret, sandbox=args.sandbox, root=project_root
        )
        try:
            token = qq.access_token()
            print(f"换到了 access_token：{credentials.mask(token)}")
            print(f"网关地址：{qq.gateway_url()}")
        except Exception as exc:  # noqa: BLE001 - 给人看的失败也要说清楚
            print(f"\n连不上或凭据不对：{exc}")
            return 1
        finally:
            qq.close()
        print("\n凭据可用。（真正跑起来：去掉 --check）")
        return 0
    if channel == "telegram":
        token, source = credentials.find(project_root, *credentials.TELEGRAM_TOKEN)
        print(f"token：{credentials.mask(token)}（{source}）")
        return 0 if token else 2
    print(f"{channel} 通道不需要凭据（或自己去 __init__ 里查）。")
    return 0
