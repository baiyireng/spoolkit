"""本机外接服务：按用户配置的配方拉起、停掉、探活。

产品内核（hub）不启 llama-server / 隧道。这里是可选快捷方式：
人在这台机器的配置里写 `[[service]]`，`spool service start` 才动手。
退出只杀**本命令写下 pid 的孩子**，不扫别人的进程。

配方名字可改。文档示例用 `llama`（本机模型）和 `tunnel`（通道 SOCKS，
配合 `--bridge-proxy`；不是模型 `--proxy`）。
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from pathlib import Path

from spoolkit import settings
from spoolkit import workspace
from spoolkit.health import probe_spec

SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
STOP_WAIT = 3.0


def pid_dir() -> Path:
    path = workspace.state_dir() / "services"
    path.mkdir(parents=True, exist_ok=True)
    return path


def pid_path(name: str) -> Path:
    return pid_dir() / f"{name}.pid"


def read_pid(name: str) -> int | None:
    path = pid_path(name)
    if not path.is_file():
        return None
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def write_pid(name: str, pid: int) -> None:
    pid_path(name).write_text(str(pid), encoding="utf-8")


def clear_pid(name: str) -> None:
    pid_path(name).unlink(missing_ok=True)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def find_service(name: str) -> settings.ExternalService | None:
    for item in settings.load_services():
        if item.name == name:
            return item
    return None


def probe_all() -> list[dict]:
    """给 `/state` 用的红绿列表。没配方就是空的。"""
    rows: list[dict] = []
    for item in settings.load_services():
        if item.health:
            ok, target, detail = probe_spec(item.health)
            rows.append(
                {
                    "ok": ok,
                    "name": item.name,
                    "target": target,
                    "detail": detail,
                }
            )
            continue
        pid = read_pid(item.name)
        if pid and pid_alive(pid):
            rows.append(
                {
                    "ok": True,
                    "name": item.name,
                    "target": "",
                    "detail": f"进程在跑（pid {pid}）",
                }
            )
        else:
            rows.append(
                {
                    "ok": False,
                    "name": item.name,
                    "target": "",
                    "detail": "没有探活地址",
                }
            )
    return rows


def start_service(name: str) -> tuple[int, str]:
    if not SAFE_NAME.match(name):
        return 2, f"配方名不合法：{name}"
    item = find_service(name)
    if item is None:
        return 2, f"没有这个配方：{name}"
    if not item.command:
        return 2, f"{name} 只探测、没有启动命令"
    existing = read_pid(name)
    if existing and pid_alive(existing):
        return 0, f"{name} 已在跑（pid {existing}）"
    if item.health:
        ok, _, _ = probe_spec(item.health, cached=False)
        if ok:
            return 0, f"{name} 已经通了，不重复拉起"
    try:
        proc = subprocess.Popen(
            list(item.command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        return 2, f"拉不起来：{exc}"
    write_pid(name, proc.pid)
    return 0, f"已拉起 {name}（pid {proc.pid}）"


def stop_service(name: str) -> tuple[int, str]:
    if not SAFE_NAME.match(name):
        return 2, f"配方名不合法：{name}"
    pid = read_pid(name)
    if pid is None:
        return 2, f"{name} 没有本命令拉起的进程"
    # 有 pid 就动手。Windows 上 os.kill(pid, 0) 会对还活着的 ssh 误报死掉，
    # 先判断再杀会变成「pid 文件清了、进程还在听端口」。
    _kill(pid)
    clear_pid(name)
    return 0, f"已停 {name}"


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
        pass
    deadline = time.time() + STOP_WAIT
    while time.time() < deadline and pid_alive(pid):
        time.sleep(0.05)
    if not pid_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return


def render_status() -> str:
    path = settings.config_path()
    lines = [f"配方表：{path}" + ("" if path.is_file() else "（还没有）"), ""]
    items = settings.load_services()
    if not items:
        lines.extend(
            [
                "没有配置外接服务。没写这张表时，隧道不探、不拉；",
                "模型仍按 provider / base_url 探活。",
                "",
                "写进上面的文件（路径、主机换成你自己的）：",
                "",
                "  # llama：本机语言模型。hub 按 provider/base_url 探活；",
                "  # 这条只方便 spool service start/stop，已经通了不重复拉起。",
                "  [[service]]",
                '  name = "llama"',
                '  command = ["llama-server", "-m", "你的模型.gguf", "--port", "8080"]',
                '  health = "http://127.0.0.1:8080/props"',
                "",
                "  # tunnel：通道出网的本机 SOCKS（ssh -D），不是模型代理。",
                "  # QQ 有 IP 白名单时借跳板固定出口；hub 配 --bridge-proxy socks5://127.0.0.1:1080。",
                "  [[service]]",
                '  name = "tunnel"',
                '  command = ["ssh", "-N", "-D", "1080", "你的跳板"]',
                '  health = "tcp:127.0.0.1:1080"',
                "",
                "command 可空：空就只探测、不替你启动。",
                "拉起：spool service start llama",
            ]
        )
        return "\n".join(lines)
    rows = {row["name"]: row for row in probe_all()}
    lines.append(f"配了 {len(items)} 个：")
    for item in items:
        row = rows.get(item.name) or {}
        flag = "通" if row.get("ok") else "不通"
        target = row.get("target") or item.health or "（无探活）"
        detail = row.get("detail") or ""
        owned = read_pid(item.name)
        extra = f"  本命令拉起 pid={owned}" if owned and pid_alive(owned) else ""
        how = "只探" if not item.command else "可启"
        lines.append(f"  - {item.name} [{how}] {flag}  {target}  {detail}{extra}")
    lines.append("")
    lines.append("拉起：spool service start <名>    停掉：spool service stop <名>")
    return "\n".join(lines)
