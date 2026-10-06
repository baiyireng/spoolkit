"""测量环境自证：开跑之前先证明"这次的读数是在一个干净环境里取的"。

存在的理由是一次**真实误判**：探针报 `A 靶可用性 0/3`，看着像模型不行；补上
`rg` 之后同一批格子变成 **3/3、3 步**。差别不在模型，在跑批环境——子进程 PATH
里没有 rg，`search_code` 直接失败（`tools/search.py:99`），8 步全花在"拿不到素材"
上。把它记成"模型不可用"，就是把环境问题洗成能力问题。

同一类污染还有第二个来源：本机回环被代理劫走。环境里一旦有
`HTTP_PROXY=http://127.0.0.1:7890`，送到 `127.0.0.1:8080` 的 `/tokenize` 会被代理
回 502，整批格子从那一刻起静默记成"未完成"（服务本身是好的）。

所以这个模块只干一件事：**把"环境不合格"变成开跑前的响亮失败（退出码 3），
并把当时的环境写成读数的一部分。** 判据三条，缺一不可：

1. 必需工具（`rg`）在 PATH 里，或者能在已知候选目录里找到；
2. 设了代理变量时，本机回环必须被 `NO_PROXY` 覆盖；
3. 源码里每个 httpx 客户端构造点都带 `trust_env=False`（httpx 0.28.x 会在
   **构造 Client** 时解析 `NO_PROXY`，`[::1]` 会直接抛 `Invalid port: ':1]'`）。

退出码沿用 `bench.EXIT_ENVIRONMENT_BLOCKED`：和"pytest 不在就别开跑"是同一个
口径，不另立一个数字。
"""

from __future__ import annotations

import ast
import os
import shutil
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from spoolkit.bench import EXIT_ENVIRONMENT_BLOCKED

#: 必需工具。少了它就别开跑——`search_code` 会整条失败，读数不可信。
REQUIRED_TOOLS: tuple[str, ...] = ("rg",)

#: 可选工具。缺了只影响一条验证线（HTML 内联 JS 靠 node），记进报告、不判死。
OPTIONAL_TOOLS: tuple[str, ...] = ("node",)

#: 代理变量（大小写都算，Windows 上两种都见过）。
PROXY_KEYS: tuple[str, ...] = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)

#: 本机回环。这三个必须不被代理接走。
LOCAL_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")

#: 补 PATH 的结果记在这个变量里，供读数落盘（沿用探针原有约定）。
PATH_ADDED_ENV = "_PROBE_PATH_ADDED"

#: 设成 1 就不去候选目录找工具。**这是给验收用的**：故意把 rg 抽掉时，
#: 探针必须失败退出，而不是从候选目录里"恰好"找回来。
NO_TOOL_DIRS_ENV = "SPOOLKIT_PROBE_NO_TOOL_DIRS"


def which(name: str, path: str | None = None) -> str | None:
    """`shutil.which` 的一层壳。留这层是为了让验收能**假装没装 rg**。"""
    return shutil.which(name, path=path)


def probe_tool_dirs() -> list[Path]:
    """`rg` 与 `node` 的候选目录。

    `tools/search.py` 依赖 rg；HTML 的内联 JS 验证依赖 node。PATH 里没有它们时，
    「找不到 rg」会被记成工具失败、「找不到 node」会被 HTML 验证器报成
    「查不了」——两条都不是模型的问题，却会把探针读数染脏。所以这里补一次 PATH。

    唯一的事实来源：`scripts/probe.py` 与
    `benchmarks/probes/orchestration/real_counterfactual.py` 都调这一个函数。
    上一轮的问题正是后者自己另建了一套沙箱环境，**没走这条补 PATH 的路**。
    """
    if os.environ.get(NO_TOOL_DIRS_ENV):
        return []
    dirs: list[Path] = []
    local = os.environ.get("LOCALAPPDATA") or ""
    if local:
        versions = Path(local) / "cursor-agent" / "versions"
        if versions.is_dir():
            for item in sorted(versions.iterdir(), reverse=True):
                if item.is_dir():
                    dirs.append(item)
        dirs += [
            Path(local) / "Programs" / "cursor" / "resources" / "app" / "node_modules" / "@vscode" / "ripgrep" / "bin",
            Path(local) / "Microsoft" / "WinGet" / "Links",
            Path(local) / "Programs" / "Microsoft VS Code" / "bin",
        ]
    dirs += [
        # 本机有 rg 但不在 PATH 里的那两个位置（实测 `where.exe rg` 找不到，
        # 而 `tests/` 与 `tools/search.py` 都依赖它）。不加这两条，
        # 探针里每一次 search_code 都会记成工具失败——那不是模型的读数。
        Path(local) / "SummerEngine" / "current" / "ripgrep" / "bin",
        Path(local) / "OpenAI" / "Codex" / "bin",
    ]
    codex_root = Path(local) / "OpenAI" / "Codex" / "bin"
    if codex_root.is_dir():
        for item in sorted(codex_root.iterdir(), reverse=True):
            if item.is_dir():
                dirs.append(item)
    dirs += [Path(r"C:\Program Files\ripgrep"), Path("/usr/local/bin"), Path("/usr/bin")]
    return [item for item in dirs if item.is_dir()]


def ensure_probe_tools(
    env: MutableMapping[str, str], tools: Sequence[str] = ("rg", "node")
) -> list[str]:
    """PATH 里没有的工具，去候选目录里找一个补上；返回补了什么（`名字@目录`）。

    只补不改：已经能解析到的不动，找不到的不假装找到。
    """
    added: list[str] = []
    for tool in tools:
        if which(tool, path=env.get("PATH", "")) is not None:
            continue
        for directory in probe_tool_dirs():
            if (directory / f"{tool}.exe").is_file() or (directory / tool).is_file():
                env["PATH"] = f"{directory}{os.pathsep}{env.get('PATH', '')}"
                added.append(f"{tool}@{directory}")
                break
    env[PATH_ADDED_ENV] = ";".join(added)
    return added


def isolate_local_http(env: MutableMapping[str, str] | None = None) -> dict[str, str]:
    """把本机回环从代理里摘出来；返回被摘掉的变量（没动过的变量不出现）。

    删掉代理变量 + 给回环写 `NO_PROXY`，两件都做：只删变量挡不住别处重新设，
    只写 `NO_PROXY` 则依赖每个客户端都读环境（httpx 0.28.x 读法还带 bug）。
    """
    target: MutableMapping[str, str] = os.environ if env is None else env
    removed: dict[str, str] = {}
    for name in PROXY_KEYS:
        value = target.pop(name, None)
        if value is not None:
            removed[name] = value
    joined = ",".join(LOCAL_HOSTS)
    target["NO_PROXY"] = joined
    target["no_proxy"] = joined
    return removed


def proxy_vars(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """当前设着的代理变量（大小写原样保留）。"""
    source = os.environ if env is None else env
    return {name: source[name] for name in PROXY_KEYS if source.get(name)}


def no_proxy_entries(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """`NO_PROXY` 里的条目（`,` 分隔，去空白、去空项）。"""
    source = os.environ if env is None else env
    raw = source.get("NO_PROXY") or source.get("no_proxy") or ""
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _covers(entries: Sequence[str], host: str) -> bool:
    for entry in entries:
        bare = entry.strip()
        if bare == "*":
            return True
        if bare.startswith("[") and bare.endswith("]"):
            # `[::1]` 在 no_proxy 里是常见写法：语义上覆盖 ::1，但带方括号的写法
            # 会让 httpx 0.28.x 解析失败（那是另一条说明，不影响这里判覆盖）。
            bare = bare[1:-1]
        if bare == host:
            return True
        # `host:port` 形式也认：no_proxy 允许带端口。**只对含一个冒号的条目生效**，
        # 否则 `::1` 会被 split 成空串——IPv6 字面量按上面的全等比较走。
        if bare.count(":") == 1 and bare.split(":", 1)[0] == host:
            return True
    return False


def proxy_problem(env: Mapping[str, str] | None = None) -> str:
    """设了代理却没绕开本机回环时，返回一句人话；没问题时返回空串。

    **只有这一种情况判不合格**：设了代理变量，且有回环主机没被 `NO_PROXY` 覆盖。
    真机证据：`HTTP_PROXY=http://127.0.0.1:7890` 加一个没覆盖回环的 `NO_PROXY`，
    `/tokenize` 被代理回 502，整批读数静默作废。
    """
    variables = proxy_vars(env)
    if not variables:
        return ""
    entries = no_proxy_entries(env)
    uncovered = [host for host in LOCAL_HOSTS if not _covers(entries, host)]
    if not uncovered:
        return ""
    found = "、".join(f"{name}={value}" for name, value in variables.items())
    return (
        f"设着代理（{found}），而 NO_PROXY（当前 {list(entries)}）没有覆盖 "
        f"{'、'.join(uncovered)}：本机回环流量会被代理接走。"
        "实测后果是 `/tokenize` 收到 502、整批读数静默变成「未完成」。"
    )


def _is_httpx_client(func: ast.expr) -> bool:
    return (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "httpx"
        and func.attr in ("Client", "AsyncClient")
    )


def audit_httpx_trust_env(root: Path | str | None = None) -> list[str]:
    """扫出**没有关掉环境代理**的 httpx 构造点，返回 `文件:行号`。

    为什么用 `ast` 而不是正则：构造点大多是跨多行的
    （`httpx.Client(\\n timeout=...,\\n trust_env=False,\\n)`），正则很容易漏。
    只认 `httpx.Client` / `httpx.AsyncClient` 两种写法；别名导入不在这里的
    守护范围，改用时把这一条也改掉。
    """
    base = Path(root) if root is not None else Path(__file__).resolve().parent
    gaps: list[str] = []
    for path in sorted(base.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):  # pragma: no cover - 坏文件不该炸审计
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_httpx_client(node.func):
                continue
            keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            trust = keywords.get("trust_env")
            if trust is None or not (isinstance(trust, ast.Constant) and trust.value is False):
                try:
                    shown = path.relative_to(base).as_posix()
                except ValueError:  # pragma: no cover - root 外的路径
                    shown = path.as_posix()
                gaps.append(f"{shown}:{node.lineno}")
    return gaps


@dataclass(frozen=True)
class EnvironmentReport:
    """一次环境自证的读数。**不进提示词**，只进日志与读数文件。"""

    tools: dict[str, str]
    missing: tuple[str, ...]
    optional_missing: tuple[str, ...]
    path_added: tuple[str, ...]
    proxy_vars: dict[str, str]
    no_proxy: tuple[str, ...]
    proxy_problem: str
    trust_env_gaps: tuple[str, ...]
    window: int | None
    effective: int | None
    tool_count: int | None
    notes: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.missing and not self.proxy_problem and not self.trust_env_gaps

    def to_dict(self) -> dict[str, object]:
        return {
            "tools": dict(self.tools),
            "missing": list(self.missing),
            "optional_missing": list(self.optional_missing),
            "path_added": list(self.path_added),
            "proxy_vars": dict(self.proxy_vars),
            "no_proxy": list(self.no_proxy),
            "proxy_problem": self.proxy_problem,
            "trust_env_gaps": list(self.trust_env_gaps),
            "window": self.window,
            "effective": self.effective,
            "tool_count": self.tool_count,
            "notes": list(self.notes),
            "ok": self.ok,
        }


def inspect(
    env: Mapping[str, str] | None = None,
    *,
    required: Sequence[str] = REQUIRED_TOOLS,
    window: int | None = None,
    tool_count: int | None = None,
    source_root: Path | str | None = None,
) -> EnvironmentReport:
    """量一次环境。**不改环境**——要改先调 `ensure_probe_tools` / `isolate_local_http`。"""
    source = os.environ if env is None else env
    path = source.get("PATH", "")
    names = list(dict.fromkeys([*required, *OPTIONAL_TOOLS]))
    tools: dict[str, str] = {}
    missing: list[str] = []
    optional_missing: list[str] = []
    for name in names:
        found = which(name, path=path)
        if found:
            tools[name] = found
        elif name in required:
            missing.append(name)
        else:
            optional_missing.append(name)

    added = tuple(part for part in (source.get(PATH_ADDED_ENV) or "").split(";") if part)
    notes: list[str] = []
    effective: int | None = None
    if window:
        try:
            from spoolkit.context.budget import Budget

            effective = Budget(window).effective()
        except Exception as exc:  # noqa: BLE001 - 量不到就如实写，不假装量到了
            notes.append(f"有效预算量不到（{type(exc).__name__}: {exc}）")
    else:
        notes.append("没给窗口，这次不报有效预算")
    if tool_count is None:
        notes.append("工具件数没量：它只能在**真实装配路径**上量，属于别的用例的守卫范围")
    if any(entry.strip().startswith("[") for entry in no_proxy_entries(source)):
        notes.append(
            "NO_PROXY 里有带方括号的写法（如 [::1]）：httpx 0.28.x 构造 Client 时"
            "会解析失败（Invalid port: ':1]'）。本仓库统一 trust_env=False 绕开，"
            "但别的读环境的进程仍会炸。"
        )

    return EnvironmentReport(
        tools=tools,
        missing=tuple(missing),
        optional_missing=tuple(optional_missing),
        path_added=added,
        proxy_vars=proxy_vars(source),
        no_proxy=no_proxy_entries(source),
        proxy_problem=proxy_problem(source),
        trust_env_gaps=tuple(audit_httpx_trust_env(source_root)),
        window=window,
        effective=effective,
        tool_count=tool_count,
        notes=tuple(notes),
    )


def format_report(report: EnvironmentReport) -> str:
    """报告正文：只写读数，不写建议（建议在 `fix_hint`）。"""
    lines = ["测量环境自证"]
    if report.tools:
        lines.append("- 工具：" + "；".join(f"{n}={p}" for n, p in report.tools.items()))
    if report.optional_missing:
        lines.append(f"- 可选工具缺失：{'、'.join(report.optional_missing)}（只影响对应的验证线）")
    if report.path_added:
        lines.append("- 补进 PATH：" + "；".join(report.path_added))
    if report.proxy_vars:
        lines.append(
            "- 代理变量：" + "；".join(f"{n}={v}" for n, v in report.proxy_vars.items())
        )
    else:
        lines.append("- 代理变量：未设置")
    if report.no_proxy:
        lines.append("- NO_PROXY：" + ",".join(report.no_proxy))
    if report.trust_env_gaps:
        lines.append("- httpx 代理防御：**有漏点** " + "、".join(report.trust_env_gaps))
    else:
        lines.append("- httpx 代理防御：全部构造点都 trust_env=False")
    if report.window:
        lines.append(f"- 窗口/有效预算：{report.window} / {report.effective}")
    if report.tool_count is not None:
        lines.append(f"- 装配工具件数：{report.tool_count}")
    for note in report.notes:
        lines.append(f"- 说明：{note}")
    lines.append("判定：" + ("合格" if report.ok else "不合格"))
    return "\n".join(lines)


def fix_hint(report: EnvironmentReport) -> str:
    """不合格时怎么办。每条都给可照做的下一步，不止说"不合格"。"""
    lines = ["怎么修："]
    if report.missing:
        lines.append(
            f"- 缺必需工具 {'、'.join(report.missing)}：装好放进 PATH。"
            f"已知候选目录都不命中时，探针不会替你猜——找不到就别开跑"
            f"（`{NO_TOOL_DIRS_ENV}=1` 是验收用的开关，正常跑别设）。"
        )
    if report.proxy_problem:
        lines.append(
            "- 本机回环被代理接走：给回环写 `NO_PROXY=127.0.0.1,localhost,::1`，"
            "或在跑分前清掉 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY。"
        )
    if report.trust_env_gaps:
        lines.append(
            "- httpx 构造点没关环境代理：" + "、".join(report.trust_env_gaps)
            + " 补上 `trust_env=False`（原因见 `src/spoolkit/net.py` 的说明）。"
        )
    if not report.missing and not report.proxy_problem and not report.trust_env_gaps:
        lines.append("- 不用修：这次环境是干净的。")
    return "\n".join(lines)


class EnvironmentUnusable(RuntimeError):
    """环境不合格。带一份 `report`，方便调用方落盘留证。"""

    def __init__(self, report: EnvironmentReport) -> None:
        super().__init__(format_report(report) + "\n" + fix_hint(report))
        self.report = report


def require(report: EnvironmentReport) -> EnvironmentReport:
    """不合格就抛。**在花钱之前调**——跑完再解释，token 已经花掉了。"""
    if not report.ok:
        raise EnvironmentUnusable(report)
    return report


def require_or_exit(report: EnvironmentReport, *, stream=None) -> int:
    """打印报告；不合格就打印建议并返回 `EXIT_ENVIRONMENT_BLOCKED`。"""
    import sys

    target = stream if stream is not None else sys.stdout
    print(format_report(report), file=target)
    if report.ok:
        return 0
    print(fix_hint(report), file=target)
    return EXIT_ENVIRONMENT_BLOCKED
