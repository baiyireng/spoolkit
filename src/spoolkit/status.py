"""`spool status`：这台机器上 spoolkit 相关服务现在什么状态，以及起停方式。

为什么要有它：用户报的原话是"**缺乏全局命令能够观察/管理服务状态**"。原先要看
本机配方得 `spool service`、看通道凭据得 `spool bridge --check`、看网页壳得去翻
另一个终端、看密钥落点得 `spool env`——而且**没有一条命令回答"现在到底有没有东西
在跑"**。这一条把机器级的那几样摆成一张表：每行给状态、地址或说明，以及
**起停方式**（最后两列是这条命令的一半价值：用户要的是"观察**和管理**"）。

三条硬约束（都有用例钉着）：

1. **只打本机地址，绝不联网**。网页壳 / 模型 / 配方探活全是 `127.0.0.1`；通道那
   几行**只报凭据从哪来，不去连平台**（真连是 `spool bridge --check` 的事）。
2. **绝不打印密钥值**：只印"从哪个文件 / 哪个环境变量来的"（`settings.resolve_secret`
   给的就是来源说明，值连拼都不拼进输出）。
3. **探测失败不冒栈**：每个探测点都是短超时（`DEFAULT_TIMEOUT`，0.5 秒）＋一句可读说明；
   整体退出码见 `render_human` 的页脚。

超时取 0.5 秒的依据：这里探的全是**回环地址**，正常监听时连接在毫秒级完成，
0.5 秒足够；而机器上五六个探测点最坏合计几秒，`spool status` 不会卡住。
真需要等模型加载完的人可以 `--timeout 5`（`--help` 里写了）。
"""

from __future__ import annotations

import json
import socket
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import httpx
from rich.markup import escape

from spoolkit import settings
from spoolkit.cli.term import state_style
from spoolkit import services
from spoolkit.bridge import credentials
from spoolkit.envfile import load_env_file
from spoolkit.kernel import pointers
from spoolkit.kernel import trust
from spoolkit.kernel import workspaces as kernel_workspaces
from spoolkit.llm.llamacpp import DEFAULT_BASE_URL, proxy_for

#: 本机探活的超时（秒）。依据见模块开头：探的是回环地址，毫秒级就该有回音。
DEFAULT_TIMEOUT = 0.5
#: `spool web` / `serve` / `hub` 的默认端口（见 app.py 里那三个子命令）。
DEFAULT_WEB_PORT = 8765
#: `--probe` 连外挂 MCP 的超时。**比本机探活宽**：它要起一个子进程（npx 之类），
#: 0.5 秒连进程都没起来，那个数字只会把"能用的服务"报成坏的。
DEFAULT_MCP_TIMEOUT = 5.0

STATE_UP = "up"
STATE_SLOW = "slow"
STATE_DOWN = "down"
STATE_UNKNOWN = "unknown"
STATE_NONE = "none"

#: 状态 → 面向人的字面。`--json` 给的是左边的英文（脚本按它判）。
STATE_LABELS: dict[str, str] = {
    STATE_UP: "up",
    STATE_SLOW: "slow",
    STATE_DOWN: "down",
    STATE_UNKNOWN: "unknown",
    STATE_NONE: "none",
}

#: 每个通道认的凭据**分组**：一组 = 一份密钥（QQ 的 AppID 与 AppSecret 是两份）。
_CHANNEL_CREDENTIALS: dict[str, tuple[tuple[str, ...], ...]] = {
    "qqbot": (credentials.QQ_APP_ID, credentials.QQ_SECRET),
    "telegram": (credentials.TELEGRAM_TOKEN,),
    "wecom": (
        credentials.WECOM_CORP_ID,
        credentials.WECOM_SECRET,
        credentials.WECOM_AGENT_ID,
    ),
    "fake": (),
}

#: 通道名 → 启动命令。认不出的通道（插件注册的）给一句通用的。
_CHANNEL_START: dict[str, str] = {
    "qqbot": "spool qq",
    "telegram": "spool hub --channel telegram",
    "wecom": "spool hub --channel wecom",
    "fake": "spool bridge --channel fake",
}


@dataclass(frozen=True)
class Row:
    """表里的一行。

    `name` 是**稳定的机器名**（`--json` 里给脚本用，例如 `model`、`web:8765`、
    `service:llama`），`label` 才是面向人的（带上了通道名 / 配方名 / 端口）。
    """

    name: str
    label: str
    state: str
    target: str = ""
    detail: str = ""
    start: str = ""
    stop: str = ""
    #: 关键项：没起就影响退出码。定义见 `render_human` 的页脚（那里印给用户）。
    critical: bool = False
    #: 这一行的状态用别的字说（`--json` 里仍是 `state`）。
    #: 为什么需要：配置 / 密钥 / 工作区那几行说的是"有没有这个东西"，
    #: 印成「在跑」会让人以为那儿有个进程（`config.toml 在跑`？）。
    state_label: str = ""


@dataclass(frozen=True)
class Report:
    rows: tuple[Row, ...]
    provider: str
    provider_source: str
    timeout: float
    elapsed_ms: int

    def critical_down(self) -> tuple[Row, ...]:
        return tuple(
            row for row in self.rows if row.critical and row.state != STATE_UP
        )

    @property
    def ok(self) -> bool:
        return not self.critical_down()


def _one_line(exc: object) -> str:
    """异常 → 一句话。**不冒栈**：回溯里那几十行对判断下一步毫无帮助。"""
    text = " ".join(str(exc).split())
    return text[:120] or type(exc).__name__


def _readable_probe(detail: str) -> str:
    """探活失败的原话是操作系统的英文（`timed out` / `Connection refused`）。

    照抄给用户看等于让他自己翻译；这里补一句可读说明，但**把原话留在括号里**——
    排查时那串英文是有用的（"被挡"与"没起"在系统层是两回事）。
    """
    text = str(detail or "").strip()
    lowered = text.lower()
    if "timed out" in lowered or "timeout" in lowered:
        return f"port timed out ({text}) — down or slow"
    if "refused" in lowered:
        return f"nothing listening ({text})"
    return text


def _probe_tcp(host: str, port: int, timeout: float) -> tuple[str, bool]:
    """试连一下端口。返回（问题说明或空串, 是不是"超时"）。

    分成两种是刻意的：**连接被拒**＝那儿没东西（"没起"）；**连接超时**＝
    那儿可能有进程但很慢（"起来了但慢"）。这两种的下一步完全不同。
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "", False
    except TimeoutError:
        return f"{host}:{port} no accept in {timeout:g}s (slow or firewalled)", True
    except OSError as exc:
        return f"{host}:{port} connect failed ({_one_line(exc)})", False


def _probe_http(url: str, timeout: float) -> tuple[str, str]:
    """两段探一个本机 HTTP 地址：先端口，再 GET。

    两段是为了分得出"没起 / 起来了但慢 / 正常"（用户对模型那一行的原话）。
    端口通但 HTTP 不回 → `slow`；`503` 也算 `slow`——llama.cpp 加载模型期间
    正是这么答的，那时候它**在跑**，只是还不能干活。
    """
    parsed = httpx.URL(url)
    host = parsed.host or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    problem, slow = _probe_tcp(host, port, timeout)
    if problem:
        return (STATE_SLOW if slow else STATE_DOWN), problem
    try:
        with httpx.Client(
            timeout=timeout, proxy=proxy_for(url), trust_env=False
        ) as client:
            response = client.get(url)
    except httpx.TimeoutException:
        return STATE_SLOW, f"port open, no HTTP in {timeout:g}s (loading?)"
    except Exception as exc:  # noqa: BLE001 - 面向人的失败也要说清楚
        return STATE_DOWN, f"port open, request failed: {_one_line(exc)}"
    if 200 <= response.status_code < 300:
        return STATE_UP, f"HTTP {response.status_code}"
    if response.status_code == 503:
        return STATE_SLOW, "HTTP 503: service up, model still loading"
    return STATE_DOWN, f"HTTP {response.status_code}"


def _recipe_for(target_url: str, recipes: list) -> object | None:
    """哪条配方管着这个地址：先按 `health` 里的 host:port 认，再按常见名字认。

    为什么要认：模型那一行的"启动命令"必须落到**用户自己的配方名**上——
    写死 `spool service start llama` 对改了名字的人是句假话。
    """
    try:
        parsed = httpx.URL(target_url)
        where = f"{parsed.host}:{parsed.port or 80}"
    except (httpx.InvalidURL, ValueError):
        where = ""
    for item in recipes:
        if where and where in (item.health or ""):
            return item
    for item in recipes:
        if item.name.lower() in {"llama", "llamacpp", "model", "llama-server"}:
            return item
    return None


def _provider_row(provider: str, source: str) -> Row:
    if not provider:
        return Row(
            name="provider",
            label="provider",
            state=STATE_DOWN,
            state_label="unset",
            target="(unset)",
            detail=(
                "unset: built-in default is fake (script replay only). "
                "Set `spool config --set provider=llamacpp` or `provider=gemini`"
            ),
            start="spool config --set provider=llamacpp",
            stop="spool config --reset <key>",
            critical=True,
        )
    if provider == "fake":
        return Row(
            name="provider",
            label="provider",
            state=STATE_DOWN,
            state_label="fake only",
            target="fake",
            detail="effective provider is fake; use llamacpp or gemini for real work",
            start="spool config --set provider=llamacpp",
            stop="spool config --reset <key>",
            critical=True,
        )
    return Row(
        name="provider",
        label="provider",
        state=STATE_UP,
        state_label="set",
        target=provider,
        detail=f"source: {source}",
        start="spool config --set provider=<name>",
        stop="spool config --reset <key>",
    )


def _model_row(
    provider: str,
    base_url: str,
    base_url_source: str,
    recipes: list,
    timeout: float,
) -> Row:
    target = (base_url or "").strip() or DEFAULT_BASE_URL
    from_default = not (base_url or "").strip()
    state, detail = _probe_http(target.rstrip("/") + "/props", timeout)
    if from_default:
        detail += " (URL from built-in default)"
    else:
        detail += f" (URL from {base_url_source})"
    critical = provider.strip().lower() == "llamacpp"
    if not critical and state != STATE_UP:
        detail += " (not llamacpp; row ignored for exit code)"
    recipe = _recipe_for(target, recipes)
    if recipe is not None:
        start = f"spool service start {recipe.name}"
        stop = f"spool service stop {recipe.name}"
    else:
        start = "(no recipe: add [[service]], see docs)"
        stop = "spool service stop <recipe>"
    return Row(
        name="model",
        label="local model",
        state=state,
        target=target,
        detail=detail,
        start=start,
        stop=stop,
        critical=critical,
    )


def _web_start(port: int) -> str:
    return "spool web" if port == DEFAULT_WEB_PORT else f"spool web --port {port}"


def _web_row(port: int, timeout: float) -> Row:
    """探一个端口上有没有网页壳，并把它的只读性与当前目标读回来。

    判据是 `GET /state` 的 JSON 形状（`readonly` 那个键）——**不是"端口开着"**：
    8765 上完全可能是别的程序。分四种答法，因为下一步完全不同：没人听
    （起一个）、有别的程序在听（换端口）、是我们的壳（在跑，看状态）、
    答的形状不对（多半是别的壳）。
    """
    url = f"http://127.0.0.1:{port}/state"
    problem, slow = _probe_tcp("127.0.0.1", port, timeout)
    if problem:
        return Row(
            name=f"web:{port}",
            label=f"web :{port}",
            state=STATE_SLOW if slow else STATE_DOWN,
            target=url,
            detail=problem,
            start=_web_start(port),
            stop="Ctrl-C (foreground; no stop entry)",
        )
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            payload = client.get(url).json()
    except httpx.TimeoutException:
        return Row(
            name=f"web:{port}",
            label=f"web :{port}",
            state=STATE_DOWN,
            target=url,
            detail=f"something on {port}, no HTTP in {timeout:g}s (not web shell?)",
            start=_web_start(port),
            stop="Ctrl-C (foreground; no stop entry)",
        )
    except Exception as exc:  # noqa: BLE001 - 面向人的失败也要说清楚
        return Row(
            name=f"web:{port}",
            label=f"web :{port}",
            state=STATE_DOWN,
            target=url,
            detail=f"something on {port}, not web shell ({_one_line(exc)})",
            start=_web_start(port),
            stop="Ctrl-C (foreground; no stop entry)",
        )
    if not isinstance(payload, dict) or "readonly" not in payload:
        return Row(
            name=f"web:{port}",
            label=f"web :{port}",
            state=STATE_DOWN,
            target=url,
            detail=f"HTTP on {port}, but /state is not spoolkit shape",
            start=_web_start(port),
            stop="Ctrl-C (foreground; no stop entry)",
        )
    target_info = payload.get("target") if isinstance(payload.get("target"), dict) else {}
    root = str((target_info or {}).get("root") or "")
    session = str((target_info or {}).get("session") or "")
    readonly = bool(payload.get("readonly"))
    bound = str(payload.get("host") or "127.0.0.1")
    if readonly:
        detail = f"readonly (bound non-loopback {bound}: /run and /confirm return 403)"
    else:
        detail = f"writable (bound {bound})"
    detail += f"; target {root or '(none)'} (session {session or '—'})"
    return Row(
        name=f"web:{port}",
        label=f"web :{port}",
        state=STATE_UP,
        target=url,
        detail=detail,
        start=_web_start(port),
        stop="Ctrl-C (foreground; no stop entry)",
    )


def _bridge_channels() -> dict[str, int]:
    """有哪几条通道值得说：用户配置里的 `[channel.*]` ＋ 放行记录里的通道。

    放行记录也要算：`channel-trust.json` 里出现过某条通道，说明它真用过——
    哪怕配置小节后来被删了，人也需要看见"这条通道有过人"。
    """
    found = {name: 0 for name in settings.known_channels()}
    for grant in trust.list_grants():
        if grant.channel:
            found[grant.channel] = found.get(grant.channel, 0) + 1
    return found


def _bridge_row(name: str, granted: int) -> Row:
    groups = _CHANNEL_CREDENTIALS.get(name, ())
    sources: list[str] = []
    missing: list[str] = []
    for group in groups:
        value, source = credentials.find(None, *group)
        if value:
            # **只收来源说明，不碰值**：`resolve_secret` 返回的第二个元素就是
            # "环境变量 X" / "用户级 <路径> 里的 KEY"，值连拼都不拼进输出。
            sources.append(source)
        else:
            missing.append(group[0])
    start = _CHANNEL_START.get(name, f"spool hub --channel {name}")
    stop = "Ctrl-C (foreground; no stop entry)"
    if missing:
        return Row(
            name=f"bridge:{name}",
            label=f"bridge {name}",
            state=STATE_DOWN,
            target=f"channel {name}",
            detail=(
                f"missing credentials ({', '.join(missing)}): write to {settings.env_path()} "
                f"via `spool env set` or edit the file"
            ),
            start=start,
            stop=stop,
        )
    if not groups:
        credential_note = "no credentials required"
    else:
        credential_note = "credentials from: " + "; ".join(sources)
    detail = credential_note
    if granted:
        detail += f"; {granted} user(s) allowed"
    detail += "; running state unknown (foreground; no run registry)"
    return Row(
        name=f"bridge:{name}",
        label=f"bridge {name}",
        state=STATE_UNKNOWN,
        target=f"channel {name}",
        detail=detail,
        start=start,
        stop=stop,
    )


def _bridge_rows() -> list[Row]:
    channels = _bridge_channels()
    if not channels:
        return [
            Row(
                name="bridge",
                label="bridge",
                state=STATE_NONE,
                target="(no channels)",
                detail="no [channel.*] and no grants; for QQ set credentials then spool qq",
                start="spool qq (or spool hub --channel <name>)",
                stop="Ctrl-C (foreground; no stop entry)",
                state_label="unset",
            )
        ]
    return [_bridge_row(name, count) for name, count in sorted(channels.items())]


def _probe_mcp(config, timeout: float) -> tuple[str, str]:
    """真连一次外挂 MCP（只在 `--probe` 时走这里）。子进程一定会被收干净。"""
    from spoolkit.mcp.client import McpClient, McpError

    client = McpClient(config, timeout=timeout)
    try:
        client.start()
        tools = client.list_tools()
    except McpError as exc:
        return STATE_DOWN, f"unreachable: {_one_line(exc)}"
    except Exception as exc:  # noqa: BLE001 - 别人的程序，什么都可能抛
        return STATE_DOWN, f"unreachable: {_one_line(exc)}"
    finally:
        client.close()
    names = ", ".join(tool.name for tool in tools) or "no tools"
    return STATE_UP, f"{len(tools)} tool(s): {names}"


def _mcp_rows(probe: bool, mcp_timeout: float) -> list[Row]:
    servers = settings.load_mcp_servers()
    if not servers:
        return [
            Row(
                name="mcp",
                label="MCP",
                state=STATE_NONE,
                target="(none)",
                detail=f"add [[mcp]] in {settings.config_path()}; see `spool mcp-servers`",
                start="(register in [[mcp]]; client spawns)",
                stop="(remove from [[mcp]])",
                state_label="none",
            )
        ]
    rows: list[Row] = []
    for item in servers:
        if probe:
            state, detail = _probe_mcp(item, mcp_timeout)
        else:
            state, detail = STATE_UNKNOWN, "registered; not probed (pass --probe)"
        rows.append(
            Row(
                name=f"mcp:{item.name}",
                label=f"MCP {item.name}",
                state=state,
                target=item.command,
                detail=(
                    (f"cmd: {item.command} {' '.join(item.args)}".strip() + "; " if item.args else "")
                    + detail
                ),
                start="spool mcp-servers --check",
                stop="(remove from [[mcp]])",
            )
        )
    return rows


def _current_workspace() -> Path | None:
    """"当前指向谁"：网页壳指针 → 命令行指针 → 登记表里最近用的那个。

    与内核顺位同一口径（**不看 cwd**，机器级入口不认当前目录）；这里的差只是
    "登记表里最近用的那个"在内核那边是不猜就报错，而这里必须给个值——
    它只是用来指一份 `.env` 的，指错了下一行就说得出指到哪儿了。
    """
    for key in ("web", "cli"):
        pointer = pointers.get(key)
        if pointer is not None and Path(pointer.workspace).is_dir():
            return Path(pointer.workspace)
    books = kernel_workspaces.known()
    return books[0] if books else None


def _workspace_rows() -> list[Row]:
    books = kernel_workspaces.list_workspaces()
    marks: list[str] = []
    for key, label in (("web", "web"), ("cli", "cli")):
        pointer = pointers.get(key)
        if pointer is None:
            continue
        exists = "" if Path(pointer.workspace).is_dir() else " (missing dir)"
        marks.append(
            f"{label} → {pointer.workspace}{exists} (session {pointer.session or '—'})"
        )
    if not books:
        return [
            Row(
                name="workspace",
                label="workspace",
                state=STATE_NONE,
                target="(none registered)",
                detail=(
                    "registry " + str(kernel_workspaces.registry_path()) + " is empty; "
                    "`spool init` in a project"
                ),
                start="spool init (or spool ws add <dir>)",
                stop="spool ws forget <name>",
                state_label="none",
            )
        ]
    return [
        Row(
            name="workspace",
            label="workspace",
            state=STATE_UP,
            state_label="ok",
            target=f"{len(books)} registered",
            detail="; ".join(marks)
            or "(no client pointers; resolve via .agent/ walk → registry)",
            start="spool init (or spool ws add <dir>)",
            stop="spool ws forget <name>",
        )
    ]


def _env_detail(path: Path) -> str:
    """`.env` status: path / presence / key count only — never values."""
    if path.is_dir():
        return "path is a directory, not a file"
    if not path.is_file():
        return "missing"
    table = load_env_file(path)
    if not table:
        return "exists but no KEY=VALUE lines parsed (encoding?)"
    return f"exists ({len(table)} keys; count only, no values)"


def _config_rows() -> list[Row]:
    """Where config/secrets live. Paths go in detail (column width clips)."""
    rows: list[Row] = []
    path = settings.config_path()
    effective = settings.effective_config_path()
    detail = f"path: {path}; " + (
        "exists" if path.is_file() else "missing (create with spool config --set)"
    )
    if effective != path and effective.is_file():
        detail += f"; legacy config still in use: {effective}"
    rows.append(
        Row(
            name="config",
            label="user config",
            state=STATE_UP if path.is_file() else STATE_NONE,
            state_label="ok" if path.is_file() else "none",
            target="config.toml (user)",
            detail=detail,
            start="spool config --set <key>=<value>",
            stop="spool config --reset <key>",
        )
    )
    env_path = settings.env_path()
    rows.append(
        Row(
            name="env:user",
            label="user .env",
            state=STATE_UP if env_path.is_file() else STATE_NONE,
            state_label="ok" if env_path.is_file() else "none",
            target="secrets (machine)",
            detail=f"path: {env_path}; {_env_detail(env_path)}; machine-level credentials",
            start="spool env set KEY=VALUE",
            stop="(edit the file)",
        )
    )
    current = _current_workspace()
    if current is not None:
        workspace_env = current / ".env"
        rows.append(
            Row(
                name="env:workspace",
                label="workspace .env",
                state=STATE_UP if workspace_env.is_file() else STATE_NONE,
                state_label="ok" if workspace_env.is_file() else "none",
                target="secrets (workspace)",
                detail=(
                    f"path: {workspace_env}; {_env_detail(workspace_env)}; "
                    f"workspace {current}"
                ),
                start="spool env import --from <file>",
                stop="(edit the file)",
            )
        )
    return rows


def _service_row(item, timeout: float) -> Row:
    """`[[service]]` 里的一条配方。与 `spool service list` **同一套口径**
    （`services.inspect_service`）：**pid 记录是判据**，探针只报状态——端口被别的
    进程占着时不许说成"我们拉起的在跑"，没有 pid 记录时也不许谎报"我们在跑"。

    被占用仍然是 `up`（那个地址上确实有进程在服务，多半就是用户自己手起的），
    只是状态列写成「被占用」、说明里点清"非本命令启动"：把它判成没起会让
    退出码变 1，而"别人替我起着"并不是"这台机器上少了什么"。
    """
    state = services.inspect_service(item, timeout=timeout, cached=False)
    probe = _readable_probe(state.probe_detail) if state.probe_detail else ""
    if state.state == services.STATE_OURS:
        # 说明里已经带了探针结果（`本命令启动的进程存活：pid …；HTTP 200`）：
        # `_readable_probe` 没补可读说明（返回值就是探针原话）时不再追加，否则同一句
        # 会印两遍；它真补了可读说明（`timed out` → 端口在超时内没接上…）时才追加。
        row_state, label = STATE_UP, ""
        detail = state.detail
        if probe and probe != state.probe_detail:
            detail += f"; {probe}"
    elif state.state == services.STATE_OCCUPIED:
        row_state, label = STATE_UP, "occupied"
        detail = state.detail + (f"; {probe}" if probe else "")
    elif state.state == services.STATE_UNKNOWN:
        row_state, label = STATE_UNKNOWN, ""
        detail = state.detail
    else:
        row_state, label = STATE_DOWN, ""
        detail = state.detail
    if item.command:
        start = f"spool service start {item.name}"
        stop = f"spool service stop {item.name}"
    else:
        start = "(probe-only)"
        stop = "(no command)"
    return Row(
        name=f"service:{item.name}",
        label=f"recipe {item.name}",
        state=row_state,
        state_label=label,
        target=state.target or item.health or "(no health URL)",
        detail=detail,
        start=start,
        stop=stop,
        critical=bool(item.command),
    )


def _web_ports(extra: list[int] | tuple[int, ...]) -> list[int]:
    """要探的网页壳端口：默认端口 ＋ 命令行点名的那几个（去重、保序）。"""
    ports: list[int] = [DEFAULT_WEB_PORT]
    for port in extra:
        if port not in ports:
            ports.append(int(port))
    return ports


def build_report(
    *,
    timeout: float = DEFAULT_TIMEOUT,
    web_ports: tuple[int, ...] | list[int] = (),
    probe_mcp: bool = False,
    mcp_timeout: float = DEFAULT_MCP_TIMEOUT,
) -> Report:
    """探一遍，攒出这张表。**顺序固定**（`--json` 的消费方按名字认，不按顺序）。"""
    started = time.monotonic()
    provider, provider_source = settings.resolve("provider")
    base_url, base_url_source = settings.resolve("base_url")
    recipes = settings.load_services()
    rows: list[Row] = [
        _provider_row(provider, provider_source),
        _model_row(provider, base_url, base_url_source, recipes, timeout),
    ]
    for port in _web_ports(web_ports):
        rows.append(_web_row(port, timeout))
    rows.extend(_bridge_rows())
    rows.extend(_mcp_rows(probe_mcp, mcp_timeout))
    rows.extend(_workspace_rows())
    rows.extend(_config_rows())
    rows.extend(_service_row(item, timeout) for item in recipes)
    return Report(
        rows=tuple(rows),
        provider=provider,
        provider_source=provider_source,
        timeout=timeout,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


def as_payload(report: Report) -> dict:
    """`--json` 的形状：字段名稳定，脚本按 `name` / `state` 判。"""
    return {
        "ok": report.ok,
        "critical_down": [row.name for row in report.critical_down()],
        "provider": {"value": report.provider, "source": report.provider_source},
        "timeout_s": report.timeout,
        "elapsed_ms": report.elapsed_ms,
        "rows": [
            {
                "name": row.name,
                "label": row.label,
                "state": row.state,
                "critical": row.critical,
                "target": row.target,
                "detail": row.detail,
                "start": row.start,
                "stop": row.stop,
            }
            for row in report.rows
        ],
    }


def render_json(report: Report) -> str:
    return json.dumps(as_payload(report), ensure_ascii=False, indent=2)


def status_exit_code(report: Report) -> int:
    """0 = 关键都在；1 = 有关键没起。用法错与配置读不出来由调用方返回 2。"""
    return 0 if report.ok else 1


def _display_width(text: str) -> int:
    """中文占两列。不按这个算，表格就会歪（而表格是否对齐全靠它）。"""
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def _clip(text: str, width: int) -> str:
    """超宽就截断加省略号：表格宁可有省略，也不能换行把行读散。"""
    text = str(text or "")
    if _display_width(text) <= width:
        return text
    kept: list[str] = []
    used = 0
    for char in text:
        step = _display_width(char)
        if used + step > width - 1:
            break
        kept.append(char)
        used += step
    return "".join(kept) + "…"


def _pad(text: str, width: int) -> str:
    text = _clip(text, width)
    return text + " " * max(1, width - _display_width(text))


def _wrap(text: str, width: int) -> list[str]:
    """按**显示宽度**折行。

    中断点优先选空格与中文标点（否则 `spool init` 会被折成 `spool i` / `nit`，
    读起来像另一个命令）；实在没有合适的中断点（例如一条比列宽还长的路径）
    才逐字折——那时宁可断开，也不能把整行挤爆。
    """
    pieces: list[str] = []
    current = ""
    used = 0
    for char in str(text or ""):
        step = _display_width(char)
        if current and used + step > width:
            cut = max(current.rfind(spot) for spot in (" ", ";", ",", ".", "；", "、", "，", "。"))
            if cut > 0 and _display_width(current[:cut]) >= width // 2:
                pieces.append(current[:cut])
                current = current[cut + 1 :]
                used = _display_width(current)
            else:
                pieces.append(current)
                current, used = "", 0
        current += char
        used += step
    if current:
        pieces.append(current)
    return pieces


#: Card layout (not a wide 5-column table): title line + indented fields,
#: blank line between cards. Long paths wrap under `note`.
_LABEL_WIDTH = 22
_DETAIL_INDENT = "    "
_DETAIL_WIDTH = 76
_FIELD_INDENT = "    "


def _title_line(row: Row) -> str:
    plain_mark = "* " if row.critical else "  "
    state = row.state_label or STATE_LABELS.get(row.state, row.state)
    plain_name = _pad(plain_mark + row.label, _LABEL_WIDTH)
    if row.critical:
        visible = "[accent]* [/]" + escape(plain_name[2:])
    else:
        visible = escape(plain_name)
    styled_state = f"[{state_style(row.state)}]{escape(state)}[/]"
    return f"{visible}  {styled_state}".rstrip()


def _field(name: str, value: str) -> list[str]:
    """One labeled field; wrap value on following lines if needed."""
    text = str(value or "").strip()
    if not text:
        return []
    label = f"[muted]{name:<7}[/]"
    prefix_plain = f"{_FIELD_INDENT}{name:<7}"
    pieces = _wrap(text, _DETAIL_WIDTH)
    if not pieces:
        return []
    lines = [f"{_FIELD_INDENT}{label}{escape(pieces[0])}"]
    cont = " " * _display_width(prefix_plain)
    lines.extend(cont + escape(piece) for piece in pieces[1:])
    return lines


def _row_lines(row: Row) -> list[str]:
    """One service card: title, then target / note / start / stop."""
    lines = [_title_line(row)]
    lines.extend(_field("target", row.target))
    lines.extend(_field("note", row.detail))
    lines.extend(_field("start", row.start))
    lines.extend(_field("stop", row.stop))
    return lines


def render_human(report: Report) -> str:
    """Spaced card list + one-line footer."""
    lines = [
        f"[muted]spoolkit status · {report.elapsed_ms}ms · timeout {report.timeout:g}s · loopback[/]",
        "",
    ]
    for index, row in enumerate(report.rows):
        if index:
            lines.append("")
        lines.extend(_row_lines(row))
    down = report.critical_down()
    lines.append("")
    if down:
        lines.append(
            "[bad]critical down:[/] " + ", ".join(escape(row.label) for row in down)
        )
    else:
        lines.append("[ok]critical ok[/]")
    footer = (
        "* critical (exit 1 if down): provider · local model (llamacpp) · "
        "[[service]] with command  ·  else informational  ·  exit 0/1/2"
    )
    lines.append(f"[muted]{escape(footer)}[/]")
    return "\n".join(lines)
