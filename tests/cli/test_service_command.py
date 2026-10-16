"""`spool service`：按本机配方拉起 / 停掉 / 列出。不写死某台机器。

这一批用例钉的是**口径**：要不要拉起，先看**本命令自己记下的 pid**；活着就不
重复拉。pid 空时再看 health 地址的 **TCP 是否已有人听**——有则拒绝 start 并
给出腾端口 / 改配方端口的指引（HTTP 探活失败如 404 也算占用，避免大模型白加载）。

**不许起长驻进程**：当配方的都是**立刻退出的短命进程**，或用本进程里的一个监听
socket 扮演"端口被占"。清理一律在 `finally` 里。
"""

import argparse
import ctypes
import json
import os
import socket
import sys
import time
from pathlib import Path

import pytest

from spoolkit import services as svc
from spoolkit.cli.app import main
from spoolkit.cli.commands.service import service_command
from spoolkit.services import (
    pid_alive,
    read_pid,
    render_status,
    start_service,
    stop_service,
)


def _recipe_block(*, name: str, command: list[str], health: str = "") -> str:
    lines = ["[[service]]", f'name = "{name}"']
    inner = ", ".join('"' + part.replace("\\", "\\\\").replace('"', '\\"') + '"' for part in command)
    lines.append(f"command = [{inner}]")
    if health:
        lines.append(f'health = "{health}"')
    return "\n".join(lines)


def _write_recipe(path: Path, *, name: str, command: list[str], health: str = "") -> None:
    path.write_text(_recipe_block(name=name, command=command, health=health) + "\n", encoding="utf-8")


def _write_recipes(path: Path, items: list[dict]) -> None:
    blocks = [_recipe_block(**item) for item in items]
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def _script(path: Path, text: str) -> Path:
    """把一段短命脚本落盘当配方命令（比塞进 TOML 的 `python -c` 好读、好排版）。"""
    path.write_text(text, encoding="utf-8")
    return path


def _hold_port() -> tuple[int, socket.socket]:
    """在 127.0.0.1 上占一个端口，扮演"端口被别人的进程占着"。

    用**本进程里的一个监听 socket**，不起子进程：收尾就是 `close()`，
    没有长驻进程要收，也不会真的去连什么跳板。
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    return int(sock.getsockname()[1]), sock


def _closed_port() -> int:
    """拿一个刚关掉的端口号（本机没人听）。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _line_for(out: str, name: str) -> str:
    """`service list` 里某条配方那一行。"""
    for line in out.splitlines():
        if line.startswith(f"  - {name} "):
            return line
    raise AssertionError(f"输出里没有 {name} 那一行：\n{out}")


def _read(path: Path) -> str:
    """日志内容（还没落盘 / 读不到都给空串，让等待循环继续试）。"""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _wait_for(pred, timeout: float) -> bool:
    """等到 pred() 为真；超时给 False（不抛，让调用方说清是哪一条不成立）。"""
    deadline = time.time() + timeout
    while True:
        if pred():
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.05)


def _console_pids() -> list[int]:
    """本进程挂在哪个控制台上（控制台上的进程 pid 表）。"""
    buf = (ctypes.c_uint32 * 16)()
    count = int(ctypes.WinDLL("kernel32").GetConsoleProcessList(buf, 16))
    return [int(buf[index]) for index in range(count)]


#: 先留一个"我真的跑过"的印记，再绑端口：绑不上就骂一句死掉——`ssh -D` 的死法。
BIND_FAIL = """\
import pathlib, socket, sys
pathlib.Path(sys.argv[2]).write_text("ran", encoding="utf-8")
sock = socket.socket()
try:
    sock.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    print("bind: Address already in use", file=sys.stderr)
    raise SystemExit(1)
raise SystemExit(0)
"""

#: 打 5 行输出然后以 3 退出：钉"退出码 + 最后几行（不是全部）"。
TAIL_FAIL = """\
import sys
for i in range(1, 6):
    print(f"line-{i}", file=sys.stderr)
raise SystemExit(3)
"""

#: 活一会儿、监听端口、**到点自己退出**的短命配方（角色：一个真的在跑的服务）。
#: 为什么自己退出：本机沙箱拦 `taskkill` / `TerminateProcess`（基线里那两条红的
#: 根因），"收尾"只能靠它自己走——测试写完标记文件，它下一次轮询就退出；
#: 最长寿命是硬上限，谁忘了收也绝不会长驻。
LISTEN_UNTIL_FLAG = """\
import ctypes, json, os, pathlib, socket, sys, time
port, flag, max_seconds = int(sys.argv[1]), pathlib.Path(sys.argv[2]), float(sys.argv[3])
sock = socket.socket()
sock.bind(("127.0.0.1", port))
sock.listen(8)
console = []
if os.name == "nt":
    buf = (ctypes.c_uint32 * 16)()
    count = int(ctypes.WinDLL("kernel32").GetConsoleProcessList(buf, 16))
    console = [int(buf[index]) for index in range(count)]
print(json.dumps({"listening": port, "console": console, "me": os.getpid()}), flush=True)
deadline = time.monotonic() + max_seconds
while time.monotonic() < deadline:
    if flag.exists():
        break
    time.sleep(0.05)
print("exiting", flush=True)
"""



def test_没配方时list说明怎么写(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(tmp_path / "config.toml"))
    args = argparse.Namespace(action="list", name="")
    assert service_command(args) == 0
    out = capsys.readouterr().out
    assert "[[service]]" in out
    assert "No [[service]]" in out or "recipes" in out
    assert "tunnel" in out
    assert "--bridge-proxy" in out
    assert "llama" in out


def test_空命令只探不启(tmp_path: Path, monkeypatch, capsys) -> None:
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(path))
    path.write_text(
        '[[service]]\nname = "tunnel"\nhealth = "tcp:127.0.0.1:1"\n',
        encoding="utf-8",
    )
    args = argparse.Namespace(action="start", name="tunnel")
    assert service_command(args) == 2
    err = capsys.readouterr().err
    assert "probe-only" in err or "no start" in err.lower()


@pytest.mark.needs("process_kill")
def test_能拉起再停掉自己的孩子(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(path))
    monkeypatch.setenv("SPOOLKIT_STATE", str(tmp_path / "state"))
    _write_recipe(
        path,
        name="sleepy",
        command=[sys.executable, "-c", "import time; time.sleep(60)"],
    )
    assert start_service("sleepy")[0] == 0
    pid = read_pid("sleepy")
    assert pid is not None and pid_alive(pid)
    time.sleep(0.2)
    assert stop_service("sleepy")[0] == 0
    deadline = time.time() + 5
    while time.time() < deadline and pid_alive(pid):
        time.sleep(0.05)
    assert not pid_alive(pid)
    assert read_pid("sleepy") is None


@pytest.mark.needs("process_kill")
def test_停的时候信号杀不掉也要能停(tmp_path: Path, monkeypatch) -> None:
    """本机 ssh 真跑：os.kill(SIGTERM) 在 Windows 上失败，pid 文件清了进程还在。"""
    import os
    import signal

    from spoolkit import services as svc

    path = tmp_path / "config.toml"
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(path))
    monkeypatch.setenv("SPOOLKIT_STATE", str(tmp_path / "state"))
    _write_recipe(
        path,
        name="sleepy",
        command=[sys.executable, "-c", "import time; time.sleep(60)"],
    )
    assert start_service("sleepy")[0] == 0
    pid = read_pid("sleepy")
    assert pid is not None
    real_kill = os.kill

    def fake_kill(target: int, sig: int) -> None:
        if sig == 0:
            return real_kill(target, 0)
        raise OSError("refused")

    monkeypatch.setattr(svc.os, "kill", fake_kill)
    try:
        assert stop_service("sleepy")[0] == 0
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                real_kill(pid, 0)
            except OSError:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("stop 没有把进程杀掉")
        assert read_pid("sleepy") is None
    finally:
        try:
            real_kill(pid, 0)
        except OSError:
            pass
        else:
            if os.name == "nt":
                import subprocess

                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                real_kill(pid, signal.SIGKILL)


def test_停没拉过的配方会说明(tmp_path: Path, monkeypatch, capsys) -> None:
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(path))
    monkeypatch.setenv("SPOOLKIT_STATE", str(tmp_path / "state"))
    path.write_text(
        '[[service]]\nname = "llama"\ncommand = ["llama-server"]\n',
        encoding="utf-8",
    )
    args = argparse.Namespace(action="stop", name="llama")
    assert service_command(args) == 2
    assert "no process started by this command" in capsys.readouterr().err


def test_入口挂上了service(capsys) -> None:
    try:
        main(["service", "--help"])
    except SystemExit as exc:
        assert exc.code == 0
    out = capsys.readouterr().out
    assert "start" in out
    assert "stop" in out


# --- 判据是 pid 记录，不是探针（2026-10-01） ---------------------------------


def test_端口被别人占着时拒绝启动并给出处理指引(tmp_path: Path, monkeypatch) -> None:
    """真现场：1080 被 Clash 占着——不要再照拉一个注定绑不上的进程。

    旧口径曾"照拉再报 exit/stderr"；加载大模型时浪费明显，且 HTTP health 返回
    404（别的服务在听）时旧探针不算 occupied。现口径：health 地址的 TCP 已有
    监听且非本命令 pid → 拒绝 start，文案引导用户腾端口或改配方端口。
    """
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(path))
    monkeypatch.setenv("SPOOLKIT_STATE", str(tmp_path / "state"))
    port, held = _hold_port()
    marker = tmp_path / "ran.txt"
    script = _script(tmp_path / "tunnel.py", BIND_FAIL)
    _write_recipe(
        path,
        name="tunnel",
        command=[sys.executable, str(script), str(port), str(marker)],
        health=f"tcp:127.0.0.1:{port}",
    )
    try:
        code, message = start_service("tunnel")
    finally:
        held.close()
    assert code == 1, message
    assert "not starting" in message
    assert "listener" in message and "not ours" in message
    assert str(port) in message
    assert "Free that port" in message or "change" in message.lower()
    assert "spool service list" in message
    assert "already running" not in message
    assert not marker.exists(), "端口被占时不得再拉子进程"
    assert read_pid("tunnel") is None


def test_http健康404但端口已有监听也要拒绝启动(tmp_path: Path, monkeypatch) -> None:
    """别的 HTTP 服务占着端口：/props → 404，旧逻辑不算 occupied，仍去 start。"""
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(path))
    monkeypatch.setenv("SPOOLKIT_STATE", str(tmp_path / "state"))
    port, held = _hold_port()
    marker = tmp_path / "ran.txt"
    script = _script(
        tmp_path / "llama.py",
        "import pathlib, sys\npathlib.Path(sys.argv[1]).write_text('ran', encoding='utf-8')\n",
    )
    _write_recipe(
        path,
        name="llama",
        command=[sys.executable, str(script), str(marker)],
        health=f"http://127.0.0.1:{port}/props",
    )
    try:
        code, message = start_service("llama")
    finally:
        held.close()
    assert code == 1, message
    assert "not starting" in message
    assert "listener" in message and "not ours" in message
    assert not marker.exists()
    assert read_pid("llama") is None


def test_list_http探活失败但tcp占用显示port_busy(tmp_path: Path, monkeypatch) -> None:
    """TCP 有人听、HTTP /props 失败时不得标 down——否则用户以为可以 start。"""
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(path))
    monkeypatch.setenv("SPOOLKIT_STATE", str(tmp_path / "state"))
    port, held = _hold_port()
    try:
        _write_recipe(
            path,
            name="llama",
            command=["llama-server"],
            health=f"http://127.0.0.1:{port}/props",
        )
        out = render_status()
    finally:
        held.close()
    line = _line_for(out, "llama")
    assert "port busy (not ours)" in line, line
    assert "[state.down]down[/]" not in line
    assert "not started by us" in line


@pytest.mark.needs("process_kill")
def test_stop后若health端口仍被占要一并清掉(tmp_path: Path, monkeypatch) -> None:
    """杀完 pid 记录后端口仍被占（脱离进程树的孤儿）→ 不得报 stopped 却留占用。"""
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(path))
    monkeypatch.setenv("SPOOLKIT_STATE", str(tmp_path / "state"))
    # 本命令"登记"的短命进程（会被 stop 杀掉）
    wrapper = _script(
        tmp_path / "wrapper.py",
        "import time\ntime.sleep(60)\n",
    )
    holder = _script(
        tmp_path / "holder.py",
        "import socket, time\n"
        "s=socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(1)\n"
        "print(s.getsockname()[1], flush=True)\n"
        "time.sleep(60)\n",
    )
    import subprocess

    wrap = subprocess.Popen(
        [sys.executable, str(wrapper)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    hold = subprocess.Popen(
        [sys.executable, str(holder)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert hold.stdout is not None
        port = int(hold.stdout.readline().strip())
        _write_recipe(
            path,
            name="llama",
            command=["llama-server"],
            health=f"tcp:127.0.0.1:{port}",
        )
        svc.write_pid("llama", wrap.pid)
        code, message = stop_service("llama")
        assert code == 0, message
        assert "stopped" in message
        assert _wait_for(lambda: wrap.poll() is not None, 3.0)
        assert _wait_for(lambda: hold.poll() is not None, 3.0), (
            f"orphan listener survived stop: {message}"
        )
        assert read_pid("llama") is None
        assert not svc.port_has_listener("127.0.0.1", port)
    finally:
        for proc in (wrap, hold):
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)


@pytest.mark.needs("process_kill")
def test_stop_force杀掉health端口上的非本命令监听(tmp_path: Path, monkeypatch) -> None:
    """empty pid 时普通 stop 拒杀；--force 按 health 端口回收监听方。"""
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(path))
    monkeypatch.setenv("SPOOLKIT_STATE", str(tmp_path / "state"))
    # 子进程占端口：force 杀的是它，不是本 pytest 进程
    port_holder = _script(
        tmp_path / "hold.py",
        "import socket, time\n"
        "s=socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(1)\n"
        "print(s.getsockname()[1], flush=True)\n"
        "time.sleep(60)\n",
    )
    proc = __import__("subprocess").Popen(
        [sys.executable, str(port_holder)],
        stdout=__import__("subprocess").PIPE,
        stderr=__import__("subprocess").DEVNULL,
        text=True,
    )
    try:
        assert proc.stdout is not None
        port_line = proc.stdout.readline().strip()
        port = int(port_line)
        _write_recipe(
            path,
            name="llama",
            command=["llama-server"],
            health=f"http://127.0.0.1:{port}/props",
        )
        code, message = stop_service("llama")
        assert code == 2
        assert "empty pid record" in message
        code, message = stop_service("llama", force=True)
        assert code == 0, message
        assert "force-stopped" in message
        assert str(port) in message
        assert _wait_for(lambda: proc.poll() is not None, 3.0), "listener still alive"
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def test_pid记录活着就不重复拉起_哪怕探针说不通(tmp_path: Path, monkeypatch) -> None:
    """判据是**自己记的 pid**：活着就说"已经在跑（pid …）"，一个都不多拉。

    这里探针（`tcp:…` 指向刚关掉的端口）说的是"不通"：改之前会照拉一个，
    改之后必须停在 pid 记录上。
    """
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(path))
    monkeypatch.setenv("SPOOLKIT_STATE", str(tmp_path / "state"))
    marker = tmp_path / "ran.txt"
    script = _script(
        tmp_path / "touch.py",
        "import pathlib, sys\npathlib.Path(sys.argv[1]).write_text('x', encoding='utf-8')\n",
    )
    _write_recipe(
        path,
        name="tunnel",
        command=[sys.executable, str(script), str(marker)],
        health=f"tcp:127.0.0.1:{_closed_port()}",
    )
    # 记录里写**本进程的 pid**：它一定活着，而且不用收尾（不会留下长驻进程）。
    svc.write_pid("tunnel", os.getpid())
    code, message = start_service("tunnel")
    assert code == 0, message
    assert "already running" in message
    assert str(os.getpid()) in message
    assert not marker.exists(), "有活着的 pid 记录却又拉起了一个进程"
    assert read_pid("tunnel") == os.getpid()


def test_子进程立刻退出要说退出码与最后几行(tmp_path: Path, monkeypatch) -> None:
    """退出码 ＋ **最后几行**（不是全部）＋ 完整输出的落点，一样都不能少。"""
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(path))
    monkeypatch.setenv("SPOOLKIT_STATE", str(tmp_path / "state"))
    script = _script(tmp_path / "tail.py", TAIL_FAIL)
    _write_recipe(path, name="boom", command=[sys.executable, str(script)])
    code, message = start_service("boom")
    assert code == 1, message
    assert "exit 3" in message
    assert "line-5" in message and "line-3" in message
    assert "line-1" not in message, "只带最后几行，别把整份输出糊上来"
    assert "full log" in message
    log = svc.log_path("boom")
    assert "line-1" in log.read_text(encoding="utf-8"), "完整输出要留在日志里"
    assert read_pid("boom") is None


def test_过期pid记录不挡着重新拉起(tmp_path: Path, monkeypatch) -> None:
    """记录里那个 pid 已经被别人复用了（指纹对不上）→ 当成"没起"，照拉。"""
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(path))
    monkeypatch.setenv("SPOOLKIT_STATE", str(tmp_path / "state"))
    script = _script(tmp_path / "tail.py", TAIL_FAIL)
    _write_recipe(path, name="boom", command=[sys.executable, str(script)])
    # pid 写活着的本进程，指纹写一个不可能的值：模拟"这个 pid 不是我们那个进程"。
    svc.write_record("boom", svc.PidRecord(pid=os.getpid(), start=1, command=()))
    code, message = start_service("boom")
    assert "already running" not in message, f"stale fingerprint: {message}"
    assert code == 1 and "exited immediately (exit 3)" in message
    assert read_pid("boom") is None


def test_pid被复用时不杀无辜进程(tmp_path: Path, monkeypatch) -> None:
    """`stop` 只杀"我们那个进程"：pid 被复用（指纹对不上）时**不许** taskkill/kill。"""
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(path))
    monkeypatch.setenv("SPOOLKIT_STATE", str(tmp_path / "state"))
    _write_recipe(path, name="tunnel", command=[sys.executable, "-c", "pass"])
    svc.write_record("tunnel", svc.PidRecord(pid=os.getpid(), start=1, command=()))
    code, message = stop_service("tunnel")
    assert code == 2, message
    assert "reused" in message and "not killing" in message, message
    assert pid_alive(os.getpid()), "那个 pid 是别人的进程，绝不能被杀"
    assert read_pid("tunnel") is None, "过期记录要清掉，别占着判据"


def test_杀不掉时pid记录留着_免得再拉起一个(tmp_path: Path, monkeypatch) -> None:
    """停止信号发了但进程没死：要**说清还在**，并且 **pid 记录留着**。

    记录就是"要不要拉起"的判据：这里清了它，下次 `start` 就会再拉起一个去抢
    同一个端口（用户看到的就是"三个不同 pid"）。这里不真起进程：把"杀"这一步
    换成空操作，用本进程的 pid 当那条活着的记录。
    """
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(path))
    monkeypatch.setenv("SPOOLKIT_STATE", str(tmp_path / "state"))
    _write_recipe(path, name="tunnel", command=[sys.executable, "-c", "pass"])
    svc.write_pid("tunnel", os.getpid())
    monkeypatch.setattr(svc, "_kill", lambda pid: None)
    monkeypatch.setattr(svc, "STOP_WAIT", 0.1)
    code, message = stop_service("tunnel")
    assert code == 1, message
    assert "still alive" in message and str(os.getpid()) in message
    assert read_pid("tunnel") == os.getpid(), "进程还在就不许清记录（清了会再拉起一个）"


def test_render_status_embeds_state_markup(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(path))
    monkeypatch.setenv("SPOOLKIT_STATE", str(tmp_path / "state"))
    closed = _closed_port()
    _write_recipes(
        path,
        [
            {
                "name": "llama",
                "command": ["llama-server"],
                "health": f"http://127.0.0.1:{closed}/props",
            },
            {"name": "mine", "command": ["x"], "health": f"tcp:127.0.0.1:{closed}"},
        ],
    )
    svc.write_pid("mine", os.getpid())
    out = render_status()
    assert "[state.down]" in out
    assert "[state.up]" in out
    mine = _line_for(out, "mine")
    assert "[start]" in mine


def test_service_list_prints_probe_and_start_via_print_out(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(path))
    path.write_text(
        '[[service]]\nname = "tunnel"\nhealth = "tcp:127.0.0.1:1"\n\n'
        + _recipe_block(name="llama", command=["llama-server"]),
        encoding="utf-8",
    )
    assert service_command(argparse.Namespace(action="list", name="")) == 0
    out = capsys.readouterr().out
    assert "[probe]" in out
    assert "[start]" in out


def test_list_三种状态分得清(tmp_path: Path, monkeypatch) -> None:
    """一行读清三种情形：运行中（本命令启动）/ 端口被占用（非本命令启动）/ 未运行。

    顺带钉住"没有 pid 记录就不许谎报我们在跑"。
    """
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(path))
    monkeypatch.setenv("SPOOLKIT_STATE", str(tmp_path / "state"))
    port, held = _hold_port()
    closed = _closed_port()
    try:
        _write_recipes(
            path,
            [
                {
                    "name": "tunnel",
                    "command": ["ssh", "-N", "-D", str(port)],
                    "health": f"tcp:127.0.0.1:{port}",
                },
                {
                    "name": "llama",
                    "command": ["llama-server"],
                    "health": f"http://127.0.0.1:{closed}/props",
                },
                {
                    "name": "mine",
                    "command": ["x"],
                    "health": f"tcp:127.0.0.1:{port}",
                },
            ],
        )
        svc.write_pid("mine", os.getpid())
        out = render_status()
    finally:
        held.close()
    tunnel = _line_for(out, "tunnel")
    llama = _line_for(out, "llama")
    mine = _line_for(out, "mine")
    assert "port busy (not ours)" in tunnel, tunnel
    assert "not started by us" in tunnel, tunnel
    assert "running (ours)" not in tunnel, f"must not claim ours: {tunnel}"
    assert "owned process alive" not in tunnel
    # listener pid may appear in occupied detail (this test holds the socket)
    assert "down" in llama, llama
    assert "down:" in llama, llama
    assert "running (ours)" in mine and f"pid {os.getpid()}" in mine, mine
    assert "在跑" not in out


# --- 拉起的子进程要脱离本控制台（2026-10-02：同控制台的 Ctrl-C 连坐） ----------


def test_拉起子进程时给了脱离控制台的creationflags(tmp_path: Path, monkeypatch) -> None:
    """结构性：Windows 上 `creationflags` 必须含 `CREATE_NEW_PROCESS_GROUP`。

    改之前这里恒红：`start_new_session=True` 是 **POSIX only**（CPython 文档写明），
    Windows 上被忽略，于是子进程与我们在同一个进程组、同一个控制台——用户在
    **同一个窗口**按 Ctrl-C，控制台把 CTRL_C_EVENT 广播给它，服务跟着死。
    `DETACHED_PROCESS` 一起钉住：它让子进程连控制台都不挂（CTRL_BREAK 也到不了）。
    """
    if os.name != "nt":
        pytest.skip("creationflags 是 Windows 专有：POSIX 走 start_new_session")
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(path))
    monkeypatch.setenv("SPOOLKIT_STATE", str(tmp_path / "state"))
    _write_recipe(path, name="tunnel", command=[sys.executable, "-c", "pass"])

    seen: list[int] = []
    real_popen = svc.subprocess.Popen

    def spy(*args, **kwargs):
        seen.append(int(kwargs.get("creationflags", 0)))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(svc.subprocess, "Popen", spy)
    start_service("tunnel")
    assert seen, "配方该被真的拉起一次"
    flags = seen[0]
    assert flags & svc._CREATE_NEW_PROCESS_GROUP, f"缺 CREATE_NEW_PROCESS_GROUP：{flags:#x}"
    assert flags & svc._DETACHED_PROCESS, f"缺 DETACHED_PROCESS：{flags:#x}"


def test_Job不放开breakaway时退掉这一位重试也要能起来(tmp_path: Path, monkeypatch) -> None:
    """`CREATE_BREAKAWAY_FROM_JOB` 拿不到时要被容忍：退掉这一位重试，不许因此起不来。

    Job 没开 `JOB_OBJECT_LIMIT_BREAKAWAY_OK` 时 `CreateProcess` 就是"拒绝访问"。
    这里把**带 breakaway 的那一次**改成那个错，看第二次（不带它）有没有真把配方拉起来。
    """
    if os.name != "nt":
        pytest.skip("creationflags 是 Windows 专有")
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(path))
    monkeypatch.setenv("SPOOLKIT_STATE", str(tmp_path / "state"))
    _write_recipe(path, name="tunnel", command=[sys.executable, "-c", "pass"])

    attempts: list[int] = []
    real_popen = svc.subprocess.Popen

    def spy(*args, **kwargs):
        flags = int(kwargs.get("creationflags", 0))
        attempts.append(flags)
        if flags & svc._CREATE_BREAKAWAY_FROM_JOB:
            raise PermissionError(13, "Access is denied")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(svc.subprocess, "Popen", spy)
    code, message = start_service("tunnel")
    assert len(attempts) == 2, f"该带 breakaway 试一次、不带它再试一次：{attempts}"
    assert attempts[0] & svc._CREATE_BREAKAWAY_FROM_JOB
    assert not attempts[1] & svc._CREATE_BREAKAWAY_FROM_JOB
    assert "start failed" not in message, message
    assert code in (0, 1), message  # 1 = 那条命令自己立刻退了，但它**真被拉起来过**


def test_拉起的子进程不在本进程的控制台上(tmp_path: Path, monkeypatch) -> None:
    """行为性证据：子进程自己报出的控制台进程表里**没有本进程**。

    Ctrl-C 是"往本控制台广播"的东西，不在这个控制台上就吃不到。真发一次
    `GenerateConsoleCtrlEvent` 在本机沙箱里做不到（它返回成功但一个进程都不受影响
    ——实测连 `ping` 这种默认处理程序都活着），所以这里钉的是那个**原因**。
    改之前恒红：不给 creationflags 的子进程继承我们的控制台，表里就有本进程。
    """
    if os.name != "nt":
        pytest.skip("控制台是 Windows 的概念")
    mine = _console_pids()
    if os.getpid() not in mine:
        pytest.skip("本进程没有控制台，这条用例证明不了什么")
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(path))
    monkeypatch.setenv("SPOOLKIT_STATE", str(tmp_path / "state"))
    script = _script(tmp_path / "console.py", LISTEN_UNTIL_FLAG)
    port = _closed_port()
    flag = tmp_path / "stop.flag"
    _write_recipe(
        path,
        name="tunnel",
        command=[sys.executable, str(script), str(port), str(flag), "5"],
        health=f"tcp:127.0.0.1:{port}",
    )
    pid = None
    try:
        start_service("tunnel")
        pid = read_pid("tunnel")
        log = svc.log_path("tunnel")
        assert _wait_for(lambda: "console" in _read(log), 5.0), _read(log)
        report = json.loads(_read(log).splitlines()[0])
        assert report["me"] != os.getpid()
        assert os.getpid() not in report["console"], (
            f"子进程还在我们的控制台里（{report['console']}），同控制台的 Ctrl-C 会连坐它"
        )
    finally:
        flag.write_text("stop", encoding="utf-8")
        _wait_for(lambda: pid is None or not pid_alive(pid), 10.0)
        svc.clear_pid("tunnel")


def test_能拉起一个监听的短命配方_列表说运行中_日志有它的输出(tmp_path: Path, monkeypatch) -> None:
    """真实走一遍：start → `list` 说"运行中（本命令启动）" → 日志里有它的输出 → 收干净。

    配方**会活一会儿**（最长 20 秒）并监听端口，所以这是"真拉起"而不是"起完就死"。
    收尾在 `finally`：本机沙箱拦 `taskkill`（基线里那两条红的根因），所以除了 `stop`
    还写一个标记文件让配方自己退出——**绝不留进程**。
    """
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(path))
    monkeypatch.setenv("SPOOLKIT_STATE", str(tmp_path / "state"))
    port = _closed_port()
    flag = tmp_path / "stop.flag"
    script = _script(tmp_path / "listen.py", LISTEN_UNTIL_FLAG)
    _write_recipe(
        path,
        name="tunnel",
        command=[sys.executable, str(script), str(port), str(flag), "20"],
        health=f"tcp:127.0.0.1:{port}",
    )
    code, message = start_service("tunnel")
    pid = read_pid("tunnel")
    reaped = False
    try:
        assert code == 0, message
        assert pid is not None and pid_alive(pid), f"配方没起来：{message}"
        line = _line_for(render_status(), "tunnel")
        assert "running (ours)" in line, line
        assert f"pid {pid}" in line, line
        assert f"127.0.0.1:{port}" in line, line
        log = svc.log_path("tunnel")
        assert _wait_for(lambda: "listening" in _read(log), 5.0), f"日志里没有它的输出：{_read(log)}"
    finally:
        flag.write_text("stop", encoding="utf-8")
        stop_service("tunnel")  # 能收就收；本机沙箱拦 taskkill，靠上面那个标记兜底
        reaped = _wait_for(lambda: pid is None or not pid_alive(pid), 20.0)
        svc.clear_pid("tunnel")
    assert reaped, "配方进程必须收干净（不许留进程）"
    assert read_pid("tunnel") is None


