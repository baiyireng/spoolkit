"""`spool status`：这台机器上 spoolkit 相关服务现在什么状态，以及怎么起 / 怎么停。

为什么要有它：用户报的原话是"**缺乏全局命令能够观察/管理服务状态**"。原先要看
本机配方得 `spool service`、看通道凭据得 `spool bridge --check`、看网页壳得去翻
另一个终端、看密钥落点得 `spool env`——而且**没有一条命令回答"现在到底有没有东西
在跑"**。这一条把机器级的那几样摆成一张表：每行给状态、地址或说明，以及
**怎么起 / 怎么停**（最后两列是这条命令的一半价值：用户要的是"观察**和管理**"）。

三条硬约束（都有用例钉着）：

1. **只打本机地址，绝不联网**。网页壳 / 模型 / 配方探活全是 `127.0.0.1`；通道那
   几行**只报凭据从哪来，不去连平台**（真连是 `spool bridge --check` 的事）。
2. **绝不打印密钥值**：只印"从哪个文件 / 哪个环境变量来的"（`settings.resolve_secret`
   给的就是来源说明，值连拼都不拼进输出）。
3. **探测失败不冒栈**：每个探测点都是短超时（`DEFAULT_TIMEOUT`，0.5 秒）＋一句人话；
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

from spoolkit import settings
from spoolkit import services
from spoolkit.bridge import credentials
from spoolkit.envfile import load_env_file
from spoolkit.health import probe_spec
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

#: 状态 → 给人看的字。`--json` 给的是左边的英文（脚本按它判）。
STATE_LABELS: dict[str, str] = {
    STATE_UP: "在跑",
    STATE_SLOW: "起来了但慢",
    STATE_DOWN: "没起",
    STATE_UNKNOWN: "看不出",
    STATE_NONE: "没有",
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

#: 通道名 → 怎么起。认不出的通道（插件注册的）给一句通用的。
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
    `service:llama`），`label` 才是给人看的（带上了通道名 / 配方名 / 端口）。
    """

    name: str
    label: str
    state: str
    target: str = ""
    detail: str = ""
    start: str = ""
    stop: str = ""
    #: 关键项：没起就影响退出码。定义见 `render_human` 的页脚（那里印给人看）。
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

    照抄给用户看等于让他自己翻译；这里补一句人话，但**把原话留在括号里**——
    排查时那串英文是有用的（"被挡"与"没起"在系统层是两回事）。
    """
    text = str(detail or "").strip()
    lowered = text.lower()
    if "timed out" in lowered or "timeout" in lowered:
        return f"端口在超时内没接上（{text}）——没起，或者很慢"
    if "refused" in lowered:
        return f"没有东西在听这个端口（{text}）"
    return text


def _probe_tcp(host: str, port: int, timeout: float) -> tuple[str, bool]:
    """试连一下端口。返回（问题说明或空串, 是不是"超时"）。

    分成两种是刻意的：**连接被拒**＝那儿没东西（"没起"）；**连接超时**＝
    那儿可能有东西但很慢（"起来了但慢"）。这两种的下一步完全不同。
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "", False
    except TimeoutError:
        return f"{host}:{port} {timeout:g}s 没接上（可能很慢，也可能被防火墙挡）", True
    except OSError as exc:
        return f"{host}:{port} 连不上（{_one_line(exc)}）", False


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
        return STATE_SLOW, f"端口在，但 {timeout:g}s 里没有 HTTP 应答（加载中？）"
    except Exception as exc:  # noqa: BLE001 - 给人看的失败也要说清楚
        return STATE_DOWN, f"端口在，请求失败：{_one_line(exc)}"
    if 200 <= response.status_code < 300:
        return STATE_UP, f"HTTP {response.status_code}"
    if response.status_code == 503:
        return STATE_SLOW, "HTTP 503：服务在，模型还没加载完"
    return STATE_DOWN, f"HTTP {response.status_code}"


def _recipe_for(target_url: str, recipes: list) -> object | None:
    """哪条配方管着这个地址：先按 `health` 里的 host:port 认，再按常见名字认。

    为什么要认：模型那一行的"怎么起"必须落到**用户自己的配方名**上——
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
            label="模型供应商",
            state=STATE_DOWN,
            state_label="没配",
            target="（未设置）",
            detail=(
                "没配过：内置默认是 fake（只会回放脚本，不能写代码）。"
                "在 `spool config --set provider=llamacpp`（本机）"
                "与 `spool config --set provider=gemini`（云端）之间选一个"
            ),
            start="spool config --set provider=llamacpp",
            stop="spool config --reset <键>",
            critical=True,
        )
    if provider == "fake":
        return Row(
            name="provider",
            label="模型供应商",
            state=STATE_DOWN,
            state_label="只有 fake",
            target="fake",
            detail="生效值是 fake（测试回放器），真实任务要 llamacpp 或 gemini",
            start="spool config --set provider=llamacpp",
            stop="spool config --reset <键>",
            critical=True,
        )
    return Row(
        name="provider",
        label="模型供应商",
        state=STATE_UP,
        state_label="已配",
        target=provider,
        detail=f"来源：{source}",
        start="spool config --set provider=<名>",
        stop="spool config --reset <键>",
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
        detail += "（地址来自内置默认）"
    else:
        detail += f"（地址来自 {base_url_source}）"
    critical = provider.strip().lower() == "llamacpp"
    if not critical and state != STATE_UP:
        detail += "（当前不按 llamacpp 跑，这一行不影响退出码）"
    recipe = _recipe_for(target, recipes)
    if recipe is not None:
        start = f"spool service start {recipe.name}"
        stop = f"spool service stop {recipe.name}"
    else:
        start = "（没配方：写 [[service]] 见文档）"
        stop = "spool service stop <配方>"
    return Row(
        name="model",
        label="本机模型",
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
    （起一个）、有别的东西在听（换端口）、是我们的壳（在跑，看状态）、
    答的东西形状不对（多半是别的壳）。
    """
    url = f"http://127.0.0.1:{port}/state"
    problem, slow = _probe_tcp("127.0.0.1", port, timeout)
    if problem:
        return Row(
            name=f"web:{port}",
            label=f"网页壳 :{port}",
            state=STATE_SLOW if slow else STATE_DOWN,
            target=url,
            detail=problem,
            start=_web_start(port),
            stop="Ctrl-C（前台，没有停入口）",
        )
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            payload = client.get(url).json()
    except httpx.TimeoutException:
        return Row(
            name=f"web:{port}",
            label=f"网页壳 :{port}",
            state=STATE_DOWN,
            target=url,
            detail=f"{port} 上有东西在听，但 {timeout:g}s 里没答 HTTP（不是网页壳？）",
            start=_web_start(port),
            stop="Ctrl-C（前台，没有停入口）",
        )
    except Exception as exc:  # noqa: BLE001 - 给人看的失败也要说清楚
        return Row(
            name=f"web:{port}",
            label=f"网页壳 :{port}",
            state=STATE_DOWN,
            target=url,
            detail=f"{port} 上有东西在听，但不是网页壳（{_one_line(exc)}）",
            start=_web_start(port),
            stop="Ctrl-C（前台，没有停入口）",
        )
    if not isinstance(payload, dict) or "readonly" not in payload:
        return Row(
            name=f"web:{port}",
            label=f"网页壳 :{port}",
            state=STATE_DOWN,
            target=url,
            detail=f"{port} 上有东西在答，但 /state 不是 spoolkit 的形状",
            start=_web_start(port),
            stop="Ctrl-C（前台，没有停入口）",
        )
    target_info = payload.get("target") if isinstance(payload.get("target"), dict) else {}
    root = str((target_info or {}).get("root") or "")
    session = str((target_info or {}).get("session") or "")
    readonly = bool(payload.get("readonly"))
    bound = str(payload.get("host") or "127.0.0.1")
    if readonly:
        detail = f"只读（绑了非回环 {bound}：/run 与 /confirm 会 403）"
    else:
        detail = f"可写（绑 {bound}）"
    detail += f"；目标 {root or '还没选'}（会话 {session or '—'}）"
    return Row(
        name=f"web:{port}",
        label=f"网页壳 :{port}",
        state=STATE_UP,
        target=url,
        detail=detail,
        start=_web_start(port),
        stop="Ctrl-C（前台，没有停入口）",
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
    stop = "Ctrl-C（前台，没有停入口）"
    if missing:
        return Row(
            name=f"bridge:{name}",
            label=f"通道桥 {name}",
            state=STATE_DOWN,
            target=f"通道 {name}",
            detail=(
                f"缺凭据（没找到 {'、'.join(missing)}）：写进 {settings.env_path()}，"
                f"用 `spool env set` 或直接编辑"
            ),
            start=start,
            stop=stop,
        )
    if not groups:
        credential_note = "这条通道不需要凭据"
    else:
        credential_note = "凭据来自：" + "；".join(sources)
    detail = credential_note
    if granted:
        detail += f"；已放行 {granted} 人"
    detail += "；在不在跑看不出（前台进程，没有运行登记）"
    return Row(
        name=f"bridge:{name}",
        label=f"通道桥 {name}",
        state=STATE_UNKNOWN,
        target=f"通道 {name}",
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
                label="通道桥",
                state=STATE_NONE,
                target="（没配任何通道）",
                detail="用户配置的 [channel.*] 与放行记录都空；要连 QQ 就先配凭据再 spool qq",
                start="spool qq（或 spool hub --channel <通道>）",
                stop="Ctrl-C（前台，没有停入口）",
                state_label="没配",
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
        return STATE_DOWN, f"连不上：{_one_line(exc)}"
    except Exception as exc:  # noqa: BLE001 - 别人的程序，什么都可能抛
        return STATE_DOWN, f"连不上：{_one_line(exc)}"
    finally:
        client.close()
    names = "、".join(tool.name for tool in tools) or "没有工具"
    return STATE_UP, f"{len(tools)} 个工具：{names}"


def _mcp_rows(probe: bool, mcp_timeout: float) -> list[Row]:
    servers = settings.load_mcp_servers()
    if not servers:
        return [
            Row(
                name="mcp",
                label="外挂 MCP",
                state=STATE_NONE,
                target="（没登记）",
                detail=f"写进 {settings.config_path()} 的 [[mcp]]；看现状 `spool mcp-servers`",
                start="（在 [[mcp]] 里登记，客户端拉起）",
                stop="（从 [[mcp]] 里删掉）",
                state_label="没登记",
            )
        ]
    rows: list[Row] = []
    for item in servers:
        if probe:
            state, detail = _probe_mcp(item, mcp_timeout)
        else:
            state, detail = STATE_UNKNOWN, "登记了，没验（加 --probe 才真连一次）"
        rows.append(
            Row(
                name=f"mcp:{item.name}",
                label=f"外挂 MCP {item.name}",
                state=state,
                target=item.command,
                detail=(
                    (f"命令：{item.command} {' '.join(item.args)}".strip() + "；" if item.args else "")
                    + detail
                ),
                start="spool mcp-servers --check",
                stop="（从 [[mcp]] 里删掉）",
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
    for key, label in (("web", "网页壳"), ("cli", "命令行")):
        pointer = pointers.get(key)
        if pointer is None:
            continue
        exists = "" if Path(pointer.workspace).is_dir() else "（目录不在了）"
        marks.append(f"{label} → {pointer.workspace}{exists}（会话 {pointer.session or '—'}）")
    if not books:
        return [
            Row(
                name="workspace",
                label="工作区",
                state=STATE_NONE,
                target="（一个都没登记）",
                detail=(
                    "登记表 " + str(kernel_workspaces.registry_path()) + " 是空的；"
                    "在项目目录里 `spool init`"
                ),
                start="spool init（或 spool ws add <目录>）",
                stop="spool ws forget <名字>",
                state_label="还没有",
            )
        ]
    return [
        Row(
            name="workspace",
            label="工作区",
            state=STATE_UP,
            state_label="有",
            target=f"登记 {len(books)} 个",
            detail="；".join(marks)
            or "（还没有任何入口指针：各入口按「往上找 .agent/ → 登记表」定）",
            start="spool init（或 spool ws add <目录>）",
            stop="spool ws forget <名字>",
        )
    ]


def _env_detail(path: Path) -> str:
    """一份 `.env` 的现状：**只有路径、存在性与键的个数，没有值**。"""
    if path.is_dir():
        return "这个路径是个目录，不是文件"
    if not path.is_file():
        return "不存在"
    table = load_env_file(path)
    if not table:
        return "存在，但一行都没解析出来（只认 KEY=VALUE 的文本；可能是编码问题）"
    return f"存在（{len(table)} 个键；只印个数，不印值）"


def _config_rows() -> list[Row]:
    """配置与密钥**落在哪、在不在**。路径放进说明行（列宽会被截，路径不能被截）。

    状态用「有 / 还没有」而不是「在跑 / 没起」：这几行说的不是进程在不在，
    印成「在跑」会让人以为 `config.toml` 是个服务。
    """
    rows: list[Row] = []
    path = settings.config_path()
    effective = settings.effective_config_path()
    detail = f"路径：{path}；" + ("存在" if path.is_file() else "不存在（用 spool config --set 建）")
    if effective != path and effective.is_file():
        detail += f"；改名前的旧配置还在用：{effective}"
    rows.append(
        Row(
            name="config",
            label="用户配置",
            state=STATE_UP if path.is_file() else STATE_NONE,
            state_label="有" if path.is_file() else "还没有",
            target="config.toml（用户级）",
            detail=detail,
            start="spool config --set <键>=<值>",
            stop="spool config --reset <键>",
        )
    )
    env_path = settings.env_path()
    rows.append(
        Row(
            name="env:user",
            label="用户级 .env",
            state=STATE_UP if env_path.is_file() else STATE_NONE,
            state_label="有" if env_path.is_file() else "还没有",
            target="密钥（机器级）",
            detail=f"路径：{env_path}；{_env_detail(env_path)}；机器级服务的凭据看这一份",
            start="spool env set KEY=VALUE",
            stop="（直接编辑那个文件）",
        )
    )
    current = _current_workspace()
    if current is not None:
        workspace_env = current / ".env"
        rows.append(
            Row(
                name="env:workspace",
                label="工作区 .env",
                state=STATE_UP if workspace_env.is_file() else STATE_NONE,
                state_label="有" if workspace_env.is_file() else "还没有",
                target="密钥（工作区）",
                detail=(
                    f"路径：{workspace_env}；{_env_detail(workspace_env)}；"
                    f"当前工作区 {current}"
                ),
                start="spool env import --from <那个文件>",
                stop="（直接编辑那个文件）",
            )
        )
    return rows


def _service_row(item, timeout: float) -> Row:
    """`[[service]]` 里的一条配方。与 `spool service` 同一套探活口径。"""
    target = item.health or "（没写探活地址）"
    if item.health:
        ok, _target, detail = probe_spec(item.health, timeout=timeout, cached=False)
        state = STATE_UP if ok else STATE_DOWN
        detail = _readable_probe(detail)
    else:
        pid = services.read_pid(item.name)
        if pid and services.pid_alive(pid):
            state, detail = STATE_UP, f"进程在跑（pid {pid}）；这条配方没有探活地址"
        else:
            state, detail = STATE_UNKNOWN, "没有探活地址，也没看到本命令拉起的 pid（看不出）"
    if item.command:
        start = f"spool service start {item.name}"
        stop = f"spool service stop {item.name}"
    else:
        start = "（这条配方只探不启）"
        stop = "（没写 command，没什么可停）"
    return Row(
        name=f"service:{item.name}",
        label=f"本机配方 {item.name}",
        state=state,
        target=target,
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
    """中文占两列。不按这个算，表格就会歪（而"一眼看清"全靠它）。"""
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
            cut = max(current.rfind(spot) for spot in (" ", "；", "、", "，", "。"))
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


#: 五列。宽度按"最要紧的内容先放得下"定：**怎么起 / 怎么停不许被截**（那是这条
#: 命令的一半价值），长说明走下面的缩进行（`_DETAIL_*`）。
#: 合计 121 列 ＋ 8 个间隔 = 129；说明行最多 `_DETAIL_INDENT` + 100 列。
_COLUMNS = ("服务", "状态", "地址 / 说明", "怎么起", "怎么停")
_WIDTHS = (19, 11, 28, 37, 26)
_GAP = "  "
_DETAIL_INDENT = "  ↳ "
_DETAIL_WIDTH = 100


def _header() -> str:
    line = _GAP.join(_pad(name, width) for name, width in zip(_COLUMNS, _WIDTHS))
    rule = "-" * _display_width(line)
    return line.rstrip() + "\n" + rule


def _row_lines(row: Row) -> list[str]:
    """一行（＋缩进的说明行）。"""
    label = ("★ " if row.critical else "  ") + row.label
    state = row.state_label or STATE_LABELS.get(row.state, row.state)
    cells = (
        label,
        state,
        row.target,
        row.start,
        row.stop,
    )
    lines = [_GAP.join(_pad(text, width) for text, width in zip(cells, _WIDTHS)).rstrip()]
    if row.detail:
        lines.extend(_DETAIL_INDENT + piece for piece in _wrap(row.detail, _DETAIL_WIDTH))
    return lines


def render_human(report: Report) -> str:
    """给人看的表 ＋ 页脚（页脚里定义"关键"是哪些、退出码是什么意思）。"""
    lines = [
        f"spoolkit 服务状态（探测用时 {report.elapsed_ms}ms，超时 {report.timeout:g}s，"
        "只打本机地址）",
        "",
        _header(),
    ]
    for row in report.rows:
        lines.extend(_row_lines(row))
    down = report.critical_down()
    lines.append("")
    if down:
        lines.append("关键项没起：" + "、".join(row.label for row in down))
    else:
        lines.append("关键项都在。")
    lines.extend(
        [
            "★ = 关键项（没起 → 退出码 1）。关键只有三处：",
            "  1) 模型供应商：没配过、或生效值是 fake（只会回放脚本）；",
            "  2) 本机模型：仅当生效 provider 是 llamacpp；",
            "  3) 用户配置 [[service]] 里写了 command 的配方（你声明了要拉起它）。",
            "其余各行只报不判：网页壳 / 通道桥按需起，外挂 MCP 与密钥落点不影响退出码。",
            "退出码：0 = 关键都在；1 = 有关键没起；2 = 用法错或配置读不出来。",
        ]
    )
    return "\n".join(lines)
