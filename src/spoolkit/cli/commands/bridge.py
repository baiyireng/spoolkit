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
from spoolkit.cli.commands.serve import _web_flags
from spoolkit.kernel import trust
from spoolkit.kernel.target import resolve_target
from spoolkit import readroots
from spoolkit import settings
from spoolkit.health import collect_rows


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
                "  2) 设环境变量 SPOOLKIT_QQ_APPID / SPOOLKIT_QQ_SECRET"
                "（改名前的 AGENTS_DEV_* 也认）\n"
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


def pairings_for(root: Path) -> Pairings:
    """这个工作区的配对记录。单独拎出来：`spool approve` 与
    `spool bridge --approve` 走的是同一份文件、同一段逻辑。"""
    return Pairings(Path(root).resolve() / ".agent" / "bridge-pairings.json")


def _adopt_legacy(project_root: Path | None, channel: str) -> int:
    """把旧配对文件里已批准的人认领成机器级放行（幂等、不删原文件）。

    由**当前这条通道**自己做：旧文件没有通道维度，只有这条通道知道该认成谁的。
    没给 `--root` 时登记表里的每个工作区各认领一次，互不串。
    """
    from spoolkit.kernel import workspaces as kernel_workspaces

    roots = [project_root] if project_root is not None else kernel_workspaces.known()
    total = 0
    for root in roots:
        if root is None:
            continue
        total += trust.adopt_legacy(root, channel)
    return total


def approve_command(root: Path, code: str) -> int:
    """批准一个配对码。返回进程退出码。"""
    pairings = pairings_for(root)
    user = pairings.approve_code(code)
    if not user:
        print(
            f"没有这个配对码：{code}\n"
            f"（看的是 {pairings.path}）",
            file=sys.stderr,
        )
        return 2
    print(f"已批准 {user}（写进 {pairings.path}）")
    return 0


def approve_anywhere(code: str, root: Path | None, ws_name: str = "") -> int:
    """`spool approve <码>`：先找机器级的码，再回落到工作区的旧配对文件。

    机器级的码带着"请求哪个工作区、哪条通道"，所以随便在哪个目录都能批；
    旧文件里的码没有这两个维度，得先知道是哪个工作区——`--ws` 点名，或者
    登记表里只有一个时就用它。
    """
    got = trust.approve_code(code)
    if got is not None:
        print(f"已批准 {got.user}（通道 {got.channel} → {got.root}）")
        return 0

    target = _pick_workspace(root, ws_name)
    if target is None:
        return 2
    return approve_command(target, code)


def _pick_workspace(root: Path | None, ws_name: str) -> Path | None:
    """要批的是旧文件里的码：得知道是哪个工作区。说不清就报错并列出候选。"""
    from spoolkit.kernel import workspaces as kernel_workspaces

    if ws_name:
        try:
            return kernel_workspaces.resolve_workspace(ws_name)
        except kernel_workspaces.WorkspaceError as exc:
            print(str(exc), file=sys.stderr)
            return None
    if root is not None:
        return root
    candidates = kernel_workspaces.known()
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        print("还没有登记过任何工作区，批不了旧式的配对码。", file=sys.stderr)
        return None
    listed = "\n".join(f"  {item}" for item in candidates)
    print(
        "登记表里有好几个工作区，说不清这个码是哪个的——"
        "用 `spool approve <码> --ws <名字>` 点名：\n"
        f"{listed}",
        file=sys.stderr,
    )
    return None


def approve_entry(args: argparse.Namespace) -> int:
    """`spool approve <码>` 的入口（顶层快捷方式，见 app.py）。

    工作区按三步定：`--root` 显式给的 → 当前目录向上找得到的 → 登记表里唯一的
    那一个。机器级的码压根不需要它（码里带着工作区），所以先试机器级。
    """
    from spoolkit import workspace as workspace_module

    explicit = str(getattr(args, "root", "") or "").strip()
    root: Path | None
    if explicit:
        root = Path(explicit).resolve()
    else:
        root = workspace_module.find_root()
    if getattr(args, "list", False):
        return list_pairings(root)
    if getattr(args, "revoke", ""):
        return revoke_command(_root_or_die(root), args.revoke)
    if getattr(args, "forget", ""):
        return forget_command(_root_or_die(root), args.forget)
    if not args.code:
        print("给个配对码，或者用 --list / --revoke <用户>。", file=sys.stderr)
        return 2
    return approve_anywhere(args.code, root, str(getattr(args, "ws", "") or ""))


def _root_or_die(root: Path | None) -> Path:
    """撤销/丢码必须落在某个工作区上。没有就退回登记表（唯一时）。"""
    if root is not None:
        return root
    picked = _pick_workspace(None, "")
    if picked is None:
        raise SystemExit(2)
    return picked


def _approve_targets(root: Path | None, include_legacy: bool = True) -> list[tuple[str, str, str]]:
    """所有待批准的码：`(码, 用户, 来源)`。机器级的在前。

    旧文件里的码只有"工作区"这一层知道——给定了工作区就只看它，没给就看登记表。
    """
    found: list[tuple[str, str, str]] = []
    for item in trust.pending():
        found.append((item.code, item.user, f"通道 {item.channel} → {item.root}"))
    if not include_legacy:
        return found
    from spoolkit.kernel import workspaces as kernel_workspaces

    scanned: set[str] = set()
    for item_root in [*([root] if root is not None else []), *kernel_workspaces.known()]:
        if item_root is None or str(item_root) in scanned:
            continue
        scanned.add(str(item_root))
        pairings = pairings_for(item_root)
        for code, user in pairings.pending:
            found.append((code, user, f"旧文件 {pairings.path}"))
    return found


def list_pairings(root: Path | None) -> int:
    """看放行了谁、还有谁在等。机器级与旧文件一起列。"""
    from spoolkit.kernel import workspaces as kernel_workspaces

    print(f"工作区：{root if root is not None else '整台机器（按通道/工作区列出）'}")
    grants = trust.list_grants()
    print(f"\n已放行（{len(grants)}）：")
    for item in grants:
        where = "、".join(str(path) for path in item.roots)
        label = f"（{item.label}）" if item.label else ""
        print(f"  {item.user}  ←  {item.channel}：{where}{label}")
    if not grants:
        print("  （没有）")

    roots = [root] if root is not None else []
    seen: set[str] = set()
    legacy_total = 0
    for candidate in [*roots, *kernel_workspaces.known()]:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        pairings = pairings_for(candidate)
        approved = pairings.approved
        legacy_total += len(approved)
        print(f"\n旧配对文件：{pairings.path}")
        print(f"  已放行（{len(approved)}）：{'、'.join(approved) or '（没有）'}")

    pending = _approve_targets(root)
    print(f"\n等批准的码（{len(pending)}）：")
    for code, user, source in pending:
        print(f"  {code}  ←  {user}　[{source}]")
    if not pending:
        print("  （没有）")
    if pending:
        print("\n批准：spool approve <码>（旧式的码要加 `--ws <名字>`）")
    if grants or legacy_total:
        print("撤销某人的放行：spool approve --revoke <用户>")
    return 0


def revoke_command(root: Path, user: str) -> int:
    pairings = pairings_for(root)
    if not pairings.revoke(user):
        print(f"这个人本来就不在放行名单里：{user}", file=sys.stderr)
        return 2
    print(f"已撤销 {user}（{pairings.path}）")
    print("他再发消息会重新拿到一个配对码。")
    return 0


def forget_command(root: Path, code: str) -> int:
    pairings = pairings_for(root)
    if not pairings.forget_code(code):
        print(f"没有这个待批准的码：{code}", file=sys.stderr)
        return 2
    print(f"已丢掉这个码：{code}（谁都没被放行）")
    return 0


def pending_notice(root: Path) -> str:
    """有配对请求在等时给一行提示（会话里也看得见）。

    为什么要它：配对码原先只出现在**桥自己的终端**上。而你可能正在另一个窗口
    跑 `spool chat`——那边什么都不知道，于是"有人要配对"这件事就悄无声息地
    过去了（真问过这个问题）。这一行把消息送到你人在的地方。
    """
    pending = pairings_for(root).pending
    if not pending:
        return ""
    lines = [f"⚠ 有 {len(pending)} 个待批准的通道配对请求（来自手机/聊天通道）："]
    for code, user in pending:
        lines.append(f"    {code}  ←  {user}")
    lines.append("  批准：/approve <码>（或在本机任意目录敲 spool approve <码>）")
    lines.append("  丢掉：/approve --forget <码>      看全部：/approve --list")
    return "\n".join(lines)


def run_extra_args(args: argparse.Namespace, project_root: Path) -> list[str]:
    """每一轮子进程要带的参数。

    `--from-channel`：远程聊天通道默认只限制**读写文件**（当前工作区；
    本机 `spool extra add` 过的关联目录会转成 `--allow-read` 带下去）。
    查资料默认开 `--web`（手机上让它查天气是常见场景）；`--no-web` 关掉。
    聊天里的 `/allow-read` 加不了目录。
    """
    # `--ask-on-stdin`：events 模式默认"无人可问就拒绝"，桥这边有人可问
    # （用户就在聊天里），所以要显式打开。
    extra: list[str] = ["--ask-on-stdin", "--from-channel"]
    for item in readroots.existing(project_root):
        extra += ["--allow-read", str(item)]
    extra += _web_flags(args)
    api = str(getattr(args, "web_search_api", "") or "").strip()
    if api:
        extra += ["--web-search-api", api]
    for domain in getattr(args, "web_allow", []) or []:
        extra += ["--web-allow", domain]
    for domain in getattr(args, "web_deny", []) or []:
        extra += ["--web-deny", domain]
    for flag, value in (
        ("--provider", getattr(args, "provider", "")),
        ("--model", getattr(args, "model", "")),
        ("--base-url", getattr(args, "base_url", "")),
        ("--script", getattr(args, "script", "")),
        ("--proxy", getattr(args, "proxy", "")),
        ("--policy", getattr(args, "policy", "")),
        ("--scope", getattr(args, "scope", "")),
    ):
        if value:
            extra += [flag, value]
    return extra


def bridge_command(args: argparse.Namespace) -> int:
    # `--root` 给了 = 这条通道**只**服务那一个工作区（收紧，也是向后兼容）；
    # 不给 = 服务整台机器，落到哪由"这条聊天的指针"决定（见 kernel/pointers）。
    project_root = Path(args.root).resolve() if str(args.root or "").strip() else None
    pairings = pairings_for(project_root) if project_root is not None else None

    if getattr(args, "check", False):
        return _check(project_root, args)

    if args.approve:
        if project_root is None:
            print(
                "`bridge --approve` 要指定工作区（旧配对文件是按工作区存的）；"
                "用 `spool approve <码> --ws <名字>`，它会先找机器级的码。",
                file=sys.stderr,
            )
            return 2
        return approve_command(project_root, args.approve)

    claimed = _adopt_legacy(project_root, str(args.channel))
    if claimed:
        print(f"已从旧配对记录认领 {claimed} 条放行（原文件没动）。", file=sys.stderr)

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
            "你用 `spool approve <码>` 放行（任意目录都能敲）；"
            "看现状用 `spool approve --list`。",
            file=sys.stderr,
        )
    if pairings is not None:
        if pairings.approved:
            print(f"已批准：{'、'.join(pairings.approved)}", file=sys.stderr)
        if pairings.pending:
            print(
                "待配对：" + "、".join(f"{user}（码 {code}）" for code, user in pairings.pending),
                file=sys.stderr,
            )
    if project_root is None:
        print(
            "这条通道服务整台机器（没给 --root）：谁被放行到哪些工作区，"
            "由 `spool approve --list` 说了算；每条聊天各指一个工作区，"
            "聊天里用 `/ws use <名字>` 换。",
            file=sys.stderr,
        )

    extra = run_extra_args(args, project_root)
    notify = settings.flag_enabled(
        "notify_disconnect", bool(getattr(args, "notify_disconnect", False))
    )

    default_session = str(getattr(args, "session", "") or "bridge")
    runner = AgentRunner(
        lambda key: resolve_target(
            workspace=project_root,
            session=None,
            client=key,
            session_fallback=default_session,
        ),
        default_session=default_session,
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
        root=project_root,
        notify_disconnect=notify,
        channel_name=str(args.channel),
        default_session=default_session,
    )

    if args.channel == "fake":
        return _fake_loop(bridge, channel, args.user)

    if notify:
        print("断开时会给最近聊天发一句。", file=sys.stderr)
    print(f"{args.channel} 通道已就绪，等消息（Ctrl-C 退出）。", file=sys.stderr)
    next_probe = 0.0
    try:
        while True:
            replies = bridge.run_once()
            for reply in replies:
                if reply.accepted:
                    print(f"· 处理了 {reply.user} 的一条消息", file=sys.stderr)
            now = time.monotonic()
            if now >= next_probe:
                next_probe = now + 5.0
                if notify:
                    bridge.notify_disconnects(collect_rows(extra))
                bridge.flush_outbox()
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

        # 代理必须传下去。**这条是踩出来的**：原先 `--check` 没传，于是它测的是
        # 直连——而 QQ 平台有 IP 白名单，直连和借云主机出去是两条不同的路。
        # 结果是"检查通过、真跑失败"，检查工具本身在骗人。
        qq = QQBotChannel(
            app_id=app_id,
            secret=secret,
            sandbox=args.sandbox,
            root=project_root,
            proxy=getattr(args, "bridge_proxy", ""),
        )
        if getattr(args, "bridge_proxy", ""):
            print(f"通道出网走代理：{args.bridge_proxy}")
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
