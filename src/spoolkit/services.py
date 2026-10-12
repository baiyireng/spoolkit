"""本机外接服务：按用户配置的配方拉起、停掉、探活。

产品内核（hub）不启 llama-server / 隧道。这里是可选快捷方式：
人在这台机器的配置里写 `[[service]]`，`spool service start` 才动手。
退出只杀**本命令写下 pid 的子进程**，不扫别人的进程。

**判据是本命令记下的 pid，不是探针**（2026-10-01 改）。探针只回答"那个端口
通不通"，而"端口通"与"是不是本命令起的"是两回事——1080 常被 Clash 这类进程
占着，拿探针当判据就会说"已经通了，不重复拉起"，于是用户以为自己有隧道、
其实一个都没起；刚 `stop` 完再说"已经通了"也是同一个根因。现在：

1. `start` 先看**自己记的 pid 还活着吗**；活着 → 明确说"已经在跑（pid …）"，
   同一条配方最多一个进程；记录里那个进程没了（或 pid 被复用）才动手拉起。
2. 探针只用来报状态：`service list` 把三种情形分开写——**运行中（本命令启动）/
   端口被占用（非本命令启动）/ 未运行**。没有 pid 记录时绝不谎报"本命令在跑"。
3. 子进程**启动后立刻退出**时，把它的退出码与最后几行输出带出来，完整输出留在
   `<用户状态目录>/services/<配方>.log`：`ssh -D 1080` 撞上被占的端口正是
   "bind: Address already in use" 然后死掉，改之前那一个字都看不到。
4. 拉起的子进程**脱离本控制台**（Windows 给 `creationflags`，POSIX 给
   `start_new_session=True`，见 `spawn_flags`）：不给的话，用户在**同一个窗口**
   按 Ctrl-C，控制台会把 CTRL_C_EVENT 广播给服务进程——"`spool qq` 之后隧道
   自己没了"就是这么来的（`start_new_session` 在 Windows 上被忽略）。

pid 记录（`<用户状态目录>/services/<配方>.pid`）里除了 pid 还留一份**进程创建
时刻的指纹**：Windows 用 `GetProcessTimes`，Linux 用 `/proc/<pid>/stat` 的第 22
个字段。没有指纹就分不出"本命令拉起的进程"和"pid 被复用后的新进程"，而后者会
让 `stop` 去杀一个无辜的进程。

配方名字可改。文档示例用 `llama`（本机模型）和 `tunnel`（通道 SOCKS，
配合 `--bridge-proxy`；不是模型 `--proxy`）。
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from rich.markup import escape

from spoolkit import settings
from spoolkit import workspace
from spoolkit.cli.term import state_style
from spoolkit.health import PROBE_TIMEOUT_SECONDS, probe_spec

SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
STOP_WAIT = 3.0

#: 拉起后盯多久：这段时间内退出就算"启动失败"（而不是"已拉起"）。
#: 0.6 秒的依据：绑不上端口、找不到主机、凭据不对这几种失败都在几十毫秒内；
#: 等更久只是让 `start` 变慢，而**正常服务**不受这段时间影响（只等它退出）。
START_GRACE = 0.6
#: 立刻退出时带出来的输出行数。带多了会把"哪一行是原因"淹没。
TAIL_LINES = 3

#: 一条配方现在的样子。三种**要分得清**的情形（`state`）：
#: - `STATE_OURS`：本命令启动的进程还活着（pid 记录就是判据）→ 运行中（本命令启动）
#: - `STATE_OCCUPIED`：探针通，但不是本命令起的——地址被别的进程占着
#: - `STATE_DOWN`：没有本命令的活进程，探针也不通 → 未运行
#: - `STATE_UNKNOWN`：这条配方没写探活地址，也没有 pid 记录 → 无法判定
STATE_OURS = "ours"
STATE_OCCUPIED = "occupied"
STATE_DOWN = "down"
STATE_UNKNOWN = "unknown"

#: 状态 → 展示词（`service list` 行里紧挨着 `[可启]` 的那一段）。
#: 三个词各自把"是不是本命令起的"说全：图例就不必再塞进表头。
STATE_LABELS: dict[str, str] = {
    STATE_OURS: "running (ours)",
    STATE_OCCUPIED: "port busy (not ours)",
    STATE_DOWN: "down",
    STATE_UNKNOWN: "unknown",
}

#: `service list` state labels → existing `state.*` theme keys (no new theme entries).
#: STATE_OCCUPIED → slow (caution yellow); `spool status` uses the same for display.
_LIST_STATE_STYLE: dict[str, str] = {
    STATE_OURS: "up",
    STATE_OCCUPIED: "slow",
    STATE_DOWN: "down",
    STATE_UNKNOWN: "unknown",
}


def _list_state_style(service_state: str) -> str:
    return state_style(_LIST_STATE_STYLE.get(service_state, "unknown"))


#: OpenProcess 的访问位（Windows）。只要这两个：判活 + 读创建时刻。
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_WAIT_TIMEOUT = 0x00000102
_ERROR_ACCESS_DENIED = 5

#: 拉起服务子进程时给的 `creationflags`（Windows）。三个位各有各的用，
#: 见 `spawn_flags` 与 `_popen_service`。
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_DETACHED_PROCESS = 0x00000008
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000

_WIN32 = None


def pid_dir() -> Path:
    path = workspace.state_dir() / "services"
    path.mkdir(parents=True, exist_ok=True)
    return path


def pid_path(name: str) -> Path:
    return pid_dir() / f"{name}.pid"


def log_path(name: str) -> Path:
    """这条配方这次拉起的输出落到哪（拉起时按新的一次截断）。"""
    return pid_dir() / f"{name}.log"


@dataclass(frozen=True)
class PidRecord:
    """本命令拉起的那条配方的登记：pid ＋ 创建时刻指纹 ＋ 当时的命令。

    `start` 为 `None` 的两种来由：升级前写下的裸 pid 文件（老记录），
    以及这个平台拿不到指纹（见 `process_start_token`）。那时退回"只按 pid 判"。
    """

    pid: int
    start: int | None = None
    command: tuple[str, ...] = ()

    @classmethod
    def for_pid(cls, pid: int, command: tuple[str, ...] = ()) -> PidRecord:
        """照**现在**这个 pid 的创建时刻做一份记录（pid 得是活的）。"""
        return cls(pid=pid, start=process_start_token(pid), command=tuple(command))

    def payload(self) -> dict:
        return {"pid": self.pid, "start": self.start, "command": list(self.command)}


def read_record(name: str) -> PidRecord | None:
    """读这条配方的登记。文件坏了 / 空的 / 没有都当"没有"。"""
    path = pid_path(name)
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    if not raw.startswith("{"):
        # 老格式：一个裸 pid（没有指纹，只能按 pid 判活）。
        try:
            pid = int(raw)
        except ValueError:
            return None
        return PidRecord(pid=pid) if pid > 0 else None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        pid = int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    start = payload.get("start")
    try:
        start = int(start) if start is not None else None
    except (TypeError, ValueError):
        start = None
    raw_command = payload.get("command")
    command = tuple(str(part) for part in raw_command) if isinstance(raw_command, list) else ()
    return PidRecord(pid=pid, start=start, command=command)


def write_record(name: str, record: PidRecord) -> None:
    pid_path(name).write_text(
        json.dumps(record.payload(), ensure_ascii=False), encoding="utf-8"
    )


def read_pid(name: str) -> int | None:
    """记录里的 pid。**判活要用 `record_alive`**——只看 pid 分不出复用。"""
    record = read_record(name)
    return record.pid if record is not None else None


def write_pid(name: str, pid: int, command: tuple[str, ...] = ()) -> None:
    """按一个**活着的** pid 写登记（指纹照现在问操作系统要）。"""
    write_record(name, PidRecord.for_pid(pid, command))


def clear_pid(name: str) -> None:
    pid_path(name).unlink(missing_ok=True)


def pid_alive(pid: int) -> bool:
    """这个 pid 上有个活着的进程吗（**不判断归属**）。

    Windows 上**不用** `os.kill(pid, 0)` 探活：POSIX 那条"0 号信号只探活"
    的规则在 Windows 上不成立——CPython 把任何信号都实现成 `OpenProcess` ＋
    `TerminateProcess`（python/cpython#125298），拿它探活有把进程探死的风险；
    本机的文件沙箱还会把那次 `TerminateProcess` 拦下来，于是它既不杀、也答不准
    活没活。只读的那一种是 `OpenProcess(SYNCHRONIZE)` ＋ `WaitForSingleObject(0)`。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        return _win32_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _win32() -> object:
    """懒加载 kernel32，并把几个调用点的签名钉死（只有 Windows 会走到）。

    签名不钉死会出错：句柄在 64 位下是 64 位的，ctypes 默认按 `int`（32 位）
    收返回值，句柄会被截断，于是"打不开句柄"变成随机事件。
    """
    global _WIN32
    if _WIN32 is None:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel32.GetProcessTimes.restype = ctypes.c_int
        kernel32.GetProcessTimes.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        _WIN32 = kernel32
    return _WIN32


def _win32_pid_alive(pid: int) -> bool:
    """`OpenProcess(SYNCHRONIZE)` ＋ `WaitForSingleObject(0)`。

    已退出 = 句柄被信号（`WAIT_OBJECT_0`）；还活着 = `WAIT_TIMEOUT`。
    打不开句柄时按"活着"办更安全：权限不够（别的进程）是打不开的常见原因，
    而认错了的代价只是"少拉一个"，反过来是"同一条配方两个进程抢同一个端口"。
    """
    import ctypes

    kernel32 = _win32()
    handle = kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
    if not handle:
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        return kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def process_start_token(pid: int) -> int | None:
    """进程创建时刻的指纹：**判 pid 有没有被系统复用**。拿不到就返回 None。"""
    if pid <= 0:
        return None
    if os.name == "nt":
        return _win32_start_token(pid)
    if sys.platform.startswith("linux"):
        return _proc_start_token(pid)
    # 别的平台（macOS 等）没有零依赖的取法：退回"只按 pid 判"（见 record_alive）。
    return None


def _win32_start_token(pid: int) -> int | None:
    import ctypes

    kernel32 = _win32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created = ctypes.c_uint64()
        exited = ctypes.c_uint64()
        kernel = ctypes.c_uint64()
        user = ctypes.c_uint64()
        ok = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        return int(created.value) if ok else None
    finally:
        kernel32.CloseHandle(handle)


def _proc_start_token(pid: int) -> int | None:
    """Linux：`/proc/<pid>/stat` 的第 22 个字段（starttime，开机以来的时钟滴答数）。

    从**最后一个** `)` 之后切：第 2 个字段是 `(comm)`，里面可以有空格甚至括号。
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    _, _, rest = raw.rpartition(")")
    fields = rest.split()
    if len(fields) < 20:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def record_alive(record: PidRecord | None) -> bool:
    """记录里那个进程**还是原来那个**吗——"要不要拉起"的唯一判据。

    拿不到指纹（老记录 / 没实现的平台 / 临时问不到）时只按 pid 判，且**宁可
    认为活着**：认错的代价是"少拉一个"，反过来是"同一条配方两个进程"。
    """
    if record is None or not pid_alive(record.pid):
        return False
    if record.start is None:
        return True
    current = process_start_token(record.pid)
    if current is None:
        return True
    return current == record.start


def find_service(name: str) -> settings.ExternalService | None:
    for item in settings.load_services():
        if item.name == name:
            return item
    return None


@dataclass(frozen=True)
class ServiceState:
    """一条配方现在的样子：`state` 是三选一（见模块开头的口径），`detail` 是一句话。"""

    name: str
    state: str
    pid: int | None
    target: str
    probe_ok: bool
    probe_detail: str
    detail: str


def inspect_service(
    item: settings.ExternalService,
    *,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    cached: bool = True,
) -> ServiceState:
    """查这条配方：**先看 pid 记录，再拿探针报状态**。两边都不改只读。"""
    record = read_record(item.name)
    ours = record_alive(record)
    pid = record.pid if ours and record is not None else None
    target = ""
    probe_ok = False
    probe_detail = "no health URL"
    if item.health:
        probe_ok, target, probe_detail = probe_spec(
            item.health, timeout=timeout, cached=cached
        )
    if ours:
        state = STATE_OURS
        tail = probe_detail if item.health else "recipe has no health URL"
        detail = f"owned process alive: pid {pid}; {tail}"
    elif item.health and probe_ok:
        # Listener present, but no live pid record → not started by us.
        state = STATE_OCCUPIED
        detail = "port has a listener, not started by us; use another port or stop that process"
    elif not item.health:
        state = STATE_UNKNOWN
        detail = "unknown: no health URL and no pid record from this command"
    else:
        state = STATE_DOWN
        detail = f"down: {probe_detail}"
    return ServiceState(
        name=item.name,
        state=state,
        pid=pid,
        target=target or (item.health or ""),
        probe_ok=probe_ok,
        probe_detail=probe_detail,
        detail=detail,
    )


def probe_all() -> list[dict]:
    """给 `/state` 用的红绿列表。没配方就是空的。

    `ok` 仍是**探针口径**（端口 / HTTP 通不通）：网页壳那一列回答的是"这个地址
    现在能不能用"。**是不是本命令起的**进 `detail`，与 `service list` 同一句话。
    """
    rows: list[dict] = []
    for item in settings.load_services():
        state = inspect_service(item)
        ok = state.probe_ok if item.health else state.state == STATE_OURS
        rows.append(
            {
                "ok": ok,
                "name": item.name,
                "target": state.target,
                "detail": state.detail,
            }
        )
    return rows


def spawn_flags() -> int:
    """拉起服务子进程时给的 Windows `creationflags`（非 Windows 返回 0）。

    为什么要有它：`start_new_session=True` 是 **POSIX only**（CPython 文档写明），
    Windows 上被忽略——不给 creationflags 的子进程与本进程同一个进程组、同一个
    控制台，用户在**同一个窗口**按 Ctrl-C，控制台把 CTRL_C_EVENT 广播给这个控制台
    上的所有进程，服务跟着一起死（"spool qq 之后隧道自己没了"就是这么来的）。

    三个位：
    - `CREATE_NEW_PROCESS_GROUP`：新进程组默认**不处理 CTRL+C**；
    - `DETACHED_PROCESS`：干脆不挂控制台，CTRL_BREAK 之类的控制台事件也到不了；
    - `CREATE_BREAKAWAY_FROM_JOB`：终端 / IDE 常把整棵进程树放进 Job 对象，Job 一关
      就连坐；这一位让子进程脱出来。它是"能带就带"，见 `_popen_service`。
    """
    if os.name != "nt":
        return 0
    return _CREATE_NEW_PROCESS_GROUP | _DETACHED_PROCESS | _CREATE_BREAKAWAY_FROM_JOB


def _popen_service(command: Sequence[str], log: Path) -> subprocess.Popen:
    """拉起一条配方：输出落进 `log`，并让它**脱离本控制台**。

    Windows 只能靠 `creationflags`（见 `spawn_flags`）；POSIX 用
    `start_new_session=True`（新会话，没有控制终端，终端里的 Ctrl-C 到不了它）。

    `CREATE_BREAKAWAY_FROM_JOB` 失败要能被容忍：Job 没开
    `JOB_OBJECT_LIMIT_BREAKAWAY_OK` 时 `CreateProcess` 会以"拒绝访问"失败，所以
    失败就**退掉这一位重试一次**——宁可脱不出 Job，也不能因此起不来。别的失败
    （命令不存在）重试得到的是同一个错，代价只有一次 `CreateProcess`。
    """
    with open(log, "wb") as sink:
        if os.name != "nt":
            return subprocess.Popen(
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        flags = spawn_flags()
        try:
            return subprocess.Popen(
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=subprocess.STDOUT,
                creationflags=flags,
            )
        except OSError:
            return subprocess.Popen(
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=subprocess.STDOUT,
                creationflags=flags & ~_CREATE_BREAKAWAY_FROM_JOB,
            )


def start_service(name: str) -> tuple[int, str]:
    if not SAFE_NAME.match(name):
        return 2, f"invalid recipe name: {name}"
    item = find_service(name)
    if item is None:
        return 2, f"unknown recipe: {name}"
    if not item.command:
        return 2, f"{name}: probe-only (no start command)"

    # Criterion: our pid record still alive → at most one process per recipe.
    record = read_record(name)
    if record is not None and record_alive(record):
        return 0, f"{name} already running (pid {record.pid}); not starting again"
    if record is not None:
        # Stale record: process gone or pid reused. Clear, then start.
        clear_pid(name)

    # Probe is status-only: is something already listening (maybe not ours)?
    occupied = ""
    if item.health:
        ok, target, _detail = probe_spec(item.health, cached=False)
        if ok:
            where = target or item.health
            occupied = f"; before start {where} already had a listener (not ours)"

    log = log_path(name)
    try:
        proc = _popen_service(item.command, log)
    except OSError as exc:
        return 2, f"start failed: {exc}"
    write_record(name, PidRecord.for_pid(proc.pid, item.command))
    code = _wait_start(proc)
    if code is not None:
        # Immediate exit: drop pid or next start will claim "already running".
        clear_pid(name)
        return 1, _exit_message(name, code, log, occupied)
    return 0, f"started {name} (pid {proc.pid}){occupied}"


def _wait_start(proc: subprocess.Popen) -> int | None:
    """盯一小会儿：退出了给退出码，还活着给 None（`poll` 同时回收子进程）。"""
    deadline = time.monotonic() + START_GRACE
    while True:
        code = proc.poll()
        if code is not None:
            return int(code)
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.05)


def _tail(path: Path, lines: int = TAIL_LINES) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    kept = [line.strip() for line in text.splitlines() if line.strip()]
    return kept[-lines:]


def _exit_message(name: str, code: int, log: Path, occupied: str) -> str:
    if code == 0:
        head = f"{name} exited immediately (exit 0: not a long-running command?)"
    else:
        head = f"{name} exited immediately (exit {code})"
    body = [head + occupied]
    tail = _tail(log)
    if tail:
        body.append(f"last {len(tail)} lines:")
        body.extend(f"    {line}" for line in tail)
    else:
        body.append("(no output)")
    body.append(f"full log: {log}")
    return "\n".join(body)


def stop_service(name: str) -> tuple[int, str]:
    if not SAFE_NAME.match(name):
        return 2, f"invalid recipe name: {name}"
    record = read_record(name)
    if record is None:
        return 2, f"{name}: no process started by this command (empty pid record)"
    if not pid_alive(record.pid):
        clear_pid(name)
        return 2, f"{name}: previous process (pid {record.pid}) is gone; cleared pid record"
    if record.start is not None:
        current = process_start_token(record.pid)
        if current is not None and current != record.start:
            # Pid reused by another process — do not kill it.
            clear_pid(name)
            return 2, (
                f"{name}: pid {record.pid} was reused (not our process); "
                "not killing; cleared pid record"
            )
    _kill(record.pid)
    if not _wait_dead(record.pid, STOP_WAIT):
        # Keep the record: it is the start gate; clearing would start a second process.
        return 1, f"{name}: stop signal sent but process still alive (pid {record.pid}); pid record kept"
    clear_pid(name)
    return 0, f"stopped {name}"


def _wait_dead(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if not pid_alive(pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _kill(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    if _wait_dead(pid, STOP_WAIT):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return


def render_status() -> str:
    path = settings.config_path()
    head = f"recipes: {path}" + ("" if path.is_file() else " (missing)")
    lines = [f"[muted]{escape(head)}[/]", ""]
    items = settings.load_services()
    if not items:
        for para in (
            "No [[service]] recipes. Without them, tunnels are not probed or started;",
            "the model is still probed via provider / base_url.",
            "",
            f"Add to {path}:",
            "",
            "  # llama: local LLM. hub probes provider/base_url;",
            "  # start/stop use our pid record (alive => do not start again).",
            "  [[service]]",
            '  name = "llama"',
            '  command = ["llama-server", "-m", "model.gguf", "--port", "8080"]',
            '  health = "http://127.0.0.1:8080/props"',
            "",
            "  # tunnel: channel SOCKS (ssh -D), not the model --proxy.",
            "  # hub: --bridge-proxy socks5://127.0.0.1:1080",
            "  [[service]]",
            '  name = "tunnel"',
            '  command = ["ssh", "-N", "-D", "1080", "jump-host"]',
            '  health = "tcp:127.0.0.1:1080"',
            "",
            "Empty command = probe only. Start: spool service start llama",
        ):
            lines.append(f"[muted]{escape(para)}[/]")
        return "\n".join(lines)
    lines.append(escape(f"{len(items)} recipe(s):"))
    for item in items:
        state = inspect_service(item, cached=False)
        how = "probe" if not item.command else "start"
        where = state.target or "(no health)"
        label = STATE_LABELS[state.state]
        styled = f"[{_list_state_style(state.state)}]{escape(label)}[/]"
        lines.append(
            f"  - {escape(item.name)} {escape(f'[{how}]')} {styled}  "
            f"{escape(where)}  {escape(state.detail)}"
        )
    lines.append("")
    lines.append(
        "[muted]"
        + escape("start: spool service start <name>    stop: spool service stop <name>")
        + "[/]"
    )
    return "\n".join(lines)
