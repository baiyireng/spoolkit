"""桥：把通道收到的消息交给 agent，再把结果发回去。

三件事，件件都有它存在的理由：

1. **白名单按用户判**，不是按内容判。按内容判（"消息里有没有敏感词"）
   等于没判——任何人都能把那句话发进来。所以 `allowed_users` 里没有的人，
   消息直接丢掉，连 agent 都不会启动。
2. **单条长度上限**。聊天通道的消息可以是任意长，而 agent 的上下文是
   有限资源：一条一万字的消息会把这一步的预算吃光。超长就丢掉并回一句
   说明——**静默丢弃最糟**，用户以为 agent 没反应。
3. **runner 注入**。桥不认识 agent（进程内循环、HTTP、MCP 都行），
   只认一个 `Callable[[str], str]`。测试因此不需要真跑一个 agent。

回复一律走通道；`run_once()` 处理一轮 poll 到的全部消息。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from spoolkit.bridge.channel import Channel, Incoming
from spoolkit.bridge.commands import handle as handle_command
from spoolkit.bridge.pairing import ALLOWLIST, OPEN, PAIRING, Pairings
from spoolkit.bridge.render import fix_markers, to_plain
from spoolkit.health import DisconnectWatch, down_line
from spoolkit.kernel import pointers, sessions, trust as trust_module
from spoolkit.kernel import workspaces
from spoolkit.kernel.target import Target, resolve_target
from spoolkit.kernel.workspaces import WorkspaceError

DEFAULT_MAX_CHARS = 4000
OUTBOX_MAX = 8
TRANSIENT_SEND = frozenset(
    {
        "ConnectTimeout",
        "ConnectError",
        "ReadTimeout",
        "WriteTimeout",
        "TimeoutException",
        "NetworkError",
        "ProxyError",
        "ConnectionError",
        "TimeoutError",
        "OSError",
    }
)

# 陌生发送者拿到配对码时的回复模板。口令要写得像人话：这条消息会原样出现在
# 别人的聊天窗口里。
PAIRING_REPLY = (
    "这条通道还不认识你。把下面这个配对码给机器的主人，"
    "他在命令行执行 `spool approve {code}` 之后你就能用了：\n{code}"
)


def _transient_send(exc: BaseException) -> bool:
    """隧道抖、握手超时这类，通了再发还有戏。平台业务错误没有。"""
    return type(exc).__name__ in TRANSIENT_SEND


@dataclass(frozen=True)
class Reply:
    """处理一条消息的结果（同时也是给测试看的凭据）。"""

    user: str
    text: str
    accepted: bool
    reason: str = ""


class Bridge:
    """收消息 → 判白名单与长度 → 交给 runner → 把回复发回通道。"""

    def __init__(
        self,
        channel: Channel,
        runner: Callable[[str], str],
        allowed_users: set[str] | frozenset[str] = frozenset(),
        max_chars: int = DEFAULT_MAX_CHARS,
        on_note: Callable[[str], None] | None = None,
        pairings: Pairings | None = None,
        access: str = PAIRING,
        root: Path | str | None = None,
        notify_disconnect: bool = False,
        disconnect_cooldown: float = 300.0,
        *,
        channel_name: str = "fake",
        default_session: str = "bridge",
        trust_mod=None,
    ) -> None:
        self.channel = channel
        self.runner = runner
        self.allowed_users = set(allowed_users)
        self.max_chars = max_chars
        self._note = on_note or (lambda text: None)
        self.pairings = pairings
        self.access = access
        # `root` 给了 = 这条通道**只**服务那一个工作区（收紧，向后兼容）；
        # 不给 = 服务整台机器，具体落到哪由"这条聊天的指针"决定。
        self.root = Path(root).resolve() if root not in (None, "") else None
        self.channel_name = str(channel_name)
        self.default_session = str(default_session)
        self._trust = trust_mod if trust_mod is not None else trust_module
        self._last_conversation = ""
        # 断开通知只发给**放过的**最近聊天，不发给还在等配对码的人。
        self._notify_conversation = ""
        self._watch = DisconnectWatch(
            enabled=notify_disconnect,
            cooldown=disconnect_cooldown,
        )
        # 发出去失败（隧道抖、握手超时）时把答复留下，通了再补发。
        # 平台业务错误（比如 11001）补发也没用，不进这里。
        self._outbox: list[tuple[str, str]] = []
        # 长任务跑完要能补一条消息回来：把"回给最近这个会话"的能力交给 runner。
        # 只有 AgentRunner 认这个接口，别的 runner（测试里的假货）不受影响。
        setter = getattr(runner, "set_notify", None)
        if callable(setter):
            setter(lambda text: self._send(text, self._last_conversation))

    def _client_key(self, user: str) -> str:
        """这条聊天的指针键。按聊天分键，所以两个群可以各指一个工作区。"""
        return f"chat:{self.channel_name}:{user}"

    def target_for(self, user: str) -> Target:
        """这条聊天现在指向哪儿。桥与测试都用它。

        **机器级（没给 `--root`）时完全不看当前目录**：内核的通用顺序
        （设计 §3.5）里"向上找 .agent/"是给 CLI 定的——"我在哪个目录敲命令"
        在那里指定性很强；而桥是长驻进程，它的 cwd 只是**启动方式的副产品**，
        跟"手机那边这条聊天想在哪干活"毫无关系。所以这条入口把"显式 > 指针 >
        登记表"用到底，没得可指就报错（由 `handle` 回一句），不拿 cwd 凑数。

        给了 `--root` 的通道只服务那一个工作区，按它解析（向后兼容）。
        """
        key = self._client_key(user)
        if self.root is None:
            pointer = pointers.get(key)
            if pointer is not None and (pointer.workspace / workspaces.MARK).is_dir():
                # 指针指着谁就是谁；把它当显式值传进去，解析剩余部分仍归内核。
                return resolve_target(
                    workspace=pointer.workspace,
                    session=None,  # 让内核的指针层给会话（同一个 key）
                    client=key,
                    session_fallback=self.default_session,
                )
            try:
                return resolve_target(
                    workspace=None,
                    client=key,
                    session_fallback=self.default_session,
                    use_cwd=False,
                )
            except WorkspaceError:
                # 登记表里也说不清：**登记表里有东西时绝不猜**（照内核的态度报错，
                # 由 `handle` 回一句）。一个都没登记过时才退回"桥的启动目录"这最后
                # 一层——那种机器上让桥整个不能用，比"默认在启动目录里干活"更糟。
                if workspaces.known():
                    raise
            return resolve_target(
                workspace=None,
                client=key,
                session_fallback=self.default_session,
            )
        return resolve_target(
            workspace=self.root,
            client=key,
            session_fallback=self.default_session,
        )

    def handle(self, message: Incoming) -> Reply:
        """处理一条消息。任何一条都不该让桥崩掉。"""
        gate, via_trust = self._admit(message)
        if gate is not None:
            return gate
        self._notify_conversation = (
            message.conversation or self._notify_conversation
        )

        text = message.text.strip()
        if not text:
            return Reply(user=message.user, text="", accepted=False, reason="空消息")

        try:
            target = self.target_for(message.user)
        except WorkspaceError as exc:
            # 说不清这条消息该落到哪个工作区：**回一句，不进 agent**。
            reply = f"{exc}\n（让主人先指定一个工作区，或发 `/ws use <名字>`。）"
            self._send(reply, message.conversation)
            return Reply(user=message.user, text=reply, accepted=False, reason="说不清工作区")
        except Exception as exc:  # noqa: BLE001 - 坏状态文件不许变成"没人理"
            # 兜底一次：机器级状态文件（`clients.json`）是人也能手改的，形状坏掉
            # 时读路径可能抛任何东西。抛出去的后果不是"报错"，而是**用户发了消息
            # 没人理**——那正是这个模块最想避免的症状，所以这里必须回一句。
            reply = (
                f"这条消息没能落到工作区上：{type(exc).__name__}: {exc}\n"
                "（多半是状态文件坏了；让主人在服务器上看一眼 `spool ws`。）"
            )
            self._send(reply, message.conversation)
            self._note(f"解析目标失败（{type(exc).__name__}）：{exc}")
            return Reply(user=message.user, text=reply, accepted=False, reason="状态坏了")

        # 准入第二步：身份过了，还要看他能不能碰**这个**工作区。
        # 只对"机器级放行"来的身份生效——`allowed_users` / `open` / `allowlist`
        # 与旧的工作区配对文件都是主人显式的口子，语义不变。
        if via_trust and not self._trust.may_use(self.channel_name, message.user, target.root):
            return self._workspace_denied(message, target)

        # 斜杠命令由桥自己处理（**在准入判定之后**：陌生人连 /help 都不该拿到）。
        command = handle_command(text, target, client_key=self._client_key(message.user))
        if command is not None:
            self._send(command, message.conversation)
            return Reply(user=message.user, text=command, accepted=True)
        if len(text) > self.max_chars:
            reply = (
                f"这条消息有 {len(text)} 字，超过上限 {self.max_chars} 字，"
                "没有交给 agent。请拆短一点再发。"
            )
            self._send(reply, message.conversation)
            return Reply(user=message.user, text=reply, accepted=False, reason="超长")

        target = self._bind_session(message.user, target, text)
        # runner 协议：只有认得 `for_client` 的 runner（真的 AgentRunner）才按
        # 客户端键取目标；测试里的假 runner 是普通可调用对象，一行都不用改。
        call = runner_for = getattr(self.runner, "for_client", None)
        call = call(self._client_key(message.user)) if callable(runner_for) else self.runner
        try:
            answer = call(text)
        except Exception as exc:  # noqa: BLE001 - 桥不该被 agent 的异常带崩
            answer = f"这一轮没跑起来：{type(exc).__name__}: {exc}"
            self._send(answer, message.conversation)
            return Reply(user=message.user, text=answer, accepted=False, reason="runner 异常")

        self._send(answer or "（没有产出）", message.conversation)
        return Reply(user=message.user, text=answer, accepted=True)

    def _bind_session(self, user: str, target: Target, text: str) -> Target:
        """第一次说话的聊天：显式开一条会话并写指针。

        "一个聊天 = 一条线程"是聊天这类界面的预期形状，而**开会话是入口面的
        显式动作**，不是解析器偷偷新建（设计 §3.4）。所以只有"指针存在、但里面
        还没有会话"时由桥开一条；连指针都没有的聊天先用这条入口的默认会话名。
        """
        from dataclasses import replace

        key = self._client_key(user)
        pointer = pointers.get(key)
        if pointer is not None and pointer.workspace == target.root:
            if pointer.session:
                sessions.ensure_title(target.root, target.session, text)
                return target
            made = sessions.create_session(target.root, title=sessions.title_from_goal(text))
            pointers.set(key, target.root, made)
            return replace(target, session=made, session_source="new")
        return target

    def _workspace_denied(self, message: Incoming, target: Target) -> Reply:
        allowed = "、".join(
            str(item) for item in self._trust.allowed_roots(self.channel_name, message.user)
        )
        reply = (
            f"你还没被放行到 {target.root}。"
            + (f"你能碰的是：{allowed}。" if allowed else "现在哪儿都没放行。")
            + "\n`/ws` 看能切的地方。"
        )
        self._send(reply, message.conversation)
        return Reply(user=message.user, text=reply, accepted=False, reason="没被放行到这个工作区")

    def _admit(self, message: Incoming) -> tuple[Reply | None, bool]:
        """准入第一步：这个人是谁。

        返回 `(不放行时的回复, 这次是不是靠机器级放行进来的)`。第二个值决定要不要
        再走"这个工作区在不在他的放行列表里"那一步（见 `handle`）。
        """
        user = message.user
        self._last_conversation = message.conversation or self._last_conversation
        if user in self.allowed_users:
            return None, False
        if self.access == OPEN:
            return None, False
        if self.access == ALLOWLIST:
            # 名单模式：名单外连码都不给（这是使用者的明确选择）。
            self._note(f"忽略未授权用户 {user}")
            return Reply(user=user, text="", accepted=False, reason="未授权"), False
        # 机器级放行（通道, 用户, 工作区列表）：换工作区不用重新配对，靠的就是它。
        if self._trust.is_allowed(self.channel_name, user):
            return None, True
        # 旧的工作区级配对文件：留着，老桥与老流程接得上。
        if self.pairings is not None and self.pairings.is_approved(user):
            return None, False
        return self._ask_for_code(message), False

    def _ask_for_code(self, message: Incoming) -> Reply:
        """陌生发送者：给一个配对码，但**不告诉 agent**。

        码记在机器级（带上它请求的工作区与通道），所以 `spool approve <码>`
        在任何目录都能批。**没有工作区可指的时候不发码**——批了也不知道该放行到
        哪儿，只会让人白等一句"已批准"。
        """
        user = message.user
        try:
            target = self.target_for(user)
        except WorkspaceError:
            reply = (
                "这条通道还不认识你，而且现在也说不清该把你放行到哪个工作区。"
                "让机器的主人先在服务器上敲 `spool ws add <目录>`，再来一次。"
            )
            self._send(reply, message.conversation)
            self._note(f"有人要配对，但没有可指的工作区：{user}")
            return Reply(user=user, text=reply, accepted=False, reason="待配对")
        code = (
            self.pairings.ensure_code(user)
            if self.pairings is not None
            # 带着工作区配对文件的桥（给了 `--root`）继续用它：老流程、
            # 老 `spool approve` 都认那一份，换成机器级的会让"批准了却不认账"。
            else self._trust.ensure_code(self.channel_name, user, target.root)
        )
        reply = PAIRING_REPLY.format(code=code)
        self._send(reply, message.conversation)
        self._note(f"有人要配对：{user}（码 {code}，工作区 {target.root}）")
        return Reply(user=user, text=reply, accepted=False, reason="待配对")

    def _render(self, text: str) -> str:
        """按**通道的渲染能力**决定要不要动这段文字（在发送侧做，不碰模型）。

        三种模式（见 `bridge/render.py` 开头那张表）：

        - `markdown`：一个字不动（真渲染 Markdown 的通道，或通道类上写了
          `markdown = True`）；
        - `fix`：只去掉客户端**不渲染**的强调标记（`**粗**`），标题/列表/围栏/
          链接/表格都留着——**手机 QQ 实测就是这一类**；
        - `plain`：折成纯文本并按窄屏排版（什么都不渲染的通道）。

        默认按 `plain`（最保守）；通道用类属性 `render_mode` 声明自己的真实能力。
        **这条别按印象填**：早先按"QQ 完全不支持 Markdown"处理，把已经在生效的
        标题、列表、链接一起折掉了，那是把排版删了。
        """
        mode = getattr(self.channel, "render_mode", None)
        if mode is None:
            mode = "markdown" if getattr(self.channel, "markdown", False) else "plain"
        if mode == "markdown":
            return text
        if mode == "fix":
            return fix_markers(text)
        return to_plain(text)

    def _send(self, text: str, conversation: str) -> None:
        """发一条消息出去。**发不出去不能把桥打死**。

        真踩过：QQ 那边回了一句 `code=11001 不支持的调用`，异常一路抛到顶层，
        整个桥进程**直接退出**——于是"手机上发了消息没人理"变成了永久状态，
        而现场只剩下一个崩栈。发失败是可能发生的事（平台没开通这个能力、被动
        回复窗口过期、消息太长……），它该是一条日志，不是一次自杀。

        真机再踩：查完天气 SOCKS 握手超时，结论已经有了但没发出去，用户一直
        等。网络抖动要留下补发；平台拒绝不要。
        """
        text = self._render(text)  # 渲染在**进 outbox 之前**，补发时不会二次处理
        try:
            self.channel.send(text, conversation)
        except Exception as exc:  # noqa: BLE001 - 通道各异，这里只要"别死"
            self._note(f"回复发送失败（{type(exc).__name__}）：{exc}")
            if _transient_send(exc):
                self._keep_outbox(text, conversation)

    def flush_outbox(self) -> int:
        """把刚才没发出去的答复再试一遍。通了发几条算几条。"""
        if not self._outbox:
            return 0
        leftover: list[tuple[str, str]] = []
        sent = 0
        for text, dest in self._outbox:
            if leftover:
                leftover.append((text, dest))
                continue
            try:
                self.channel.send(text, dest)
            except Exception as exc:  # noqa: BLE001
                self._note(f"补发仍失败（{type(exc).__name__}）：{exc}")
                leftover.append((text, dest))
                continue
            sent += 1
            self._note("补发了一条刚才没发出去的回复")
        self._outbox = leftover
        return sent

    def _keep_outbox(self, text: str, conversation: str) -> None:
        self._outbox.append((text, conversation))
        overflow = len(self._outbox) - OUTBOX_MAX
        if overflow > 0:
            del self._outbox[:overflow]

    def run_once(self) -> list[Reply]:
        """把当前能取到的消息都处理一遍。"""
        replies: list[Reply] = []
        for message in self.channel.poll():
            try:
                replies.append(self.handle(message))
            except Exception as exc:  # noqa: BLE001 - 一条坏消息不该终止长驻进程
                self._note(f"处理消息失败（{type(exc).__name__}）：{exc}")
        return replies

    def notify_disconnects(self, rows: list[dict]) -> list[str]:
        """通→断时给最近聊天发一句。没开开关、没人说过话、冷却期内都不发。"""
        sent: list[str] = []
        dest = self._notify_conversation
        for row in self._watch.edges(rows):
            text = down_line(row)
            if not dest:
                self._note("断开了，但还没有最近聊天可通知")
                continue
            self._send(text, dest)
            sent.append(text)
        return sent
