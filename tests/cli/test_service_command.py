"""`spool service`：按本机配方拉起 / 停掉 / 列出。不写死某台机器。

这一批用例钉的是**口径**（2026-10-01 改）：要不要拉起，看**本命令自己记下的
pid**，不看探针——端口被别人（Clash 之类）占着时探针也是"通"，拿它当判据就会
"已经通了，不重复拉起"，于是用户以为自己有隧道、其实一个子进程都没起。

**不许起长驻进程**：当配方的都是**立刻退出的短命进程**，或用本进程里的一个监听
socket 扮演"端口被占"。清理一律在 `finally` 里。
"""

import argparse
import os
import socket
import sys
import time
from pathlib import Path

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



def test_没配方时list说明怎么写(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(tmp_path / "config.toml"))
    args = argparse.Namespace(action="list", name="")
    assert service_command(args) == 0
    out = capsys.readouterr().out
    assert "[[service]]" in out
    assert "不探" in out or "没有" in out
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
    assert "启动命令" in err or "只探" in err


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
    assert "拉起" in capsys.readouterr().err


def test_入口挂上了service(capsys) -> None:
    try:
        main(["service", "--help"])
    except SystemExit as exc:
        assert exc.code == 0
    out = capsys.readouterr().out
    assert "start" in out
    assert "stop" in out


# --- 判据是 pid 记录，不是探针（2026-10-01） ---------------------------------


def test_端口被别人占着也要照拉_拉不起来要说退出码与输出(tmp_path: Path, monkeypatch) -> None:
    """真现场：1080 被 Clash 占着，`ssh -D 1080` 绑不上就死。

    改之前：探针说"通" → 回一句"已经通了，不重复拉起"，**一个子进程都没起**，
    用户以为自己有隧道。改之后：pid 记录里没有活着的进程 → 照拉 → 子进程真跑过
    （印记文件），并且把它的退出码与 stderr 带出来。
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
    assert "启动后立刻退出" in message
    assert "退出码 1" in message
    assert "bind: Address already in use" in message
    assert "不重复拉起" not in message
    assert "上就有东西在听" in message  # 说清"端口被占"，不是只报 timed out
    assert marker.is_file(), "端口被占着时也必须真去拉一次（判据是 pid，不是探针）"
    assert read_pid("tunnel") is None, "立刻退出的子进程不能留下 pid 记录"


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
    assert "已经在跑" in message
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
    assert "退出码 3" in message
    assert "line-5" in message and "line-3" in message
    assert "line-1" not in message, "只带最后几行，别把整份输出糊上来"
    assert "完整输出" in message
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
    assert "已经在跑" not in message, f"指纹对不上就不是我们的进程：{message}"
    assert code == 1 and "启动后立刻退出（退出码 3）" in message
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
    assert "复用" in message or "不是我们拉起的" in message
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
    assert "还在" in message and str(os.getpid()) in message
    assert read_pid("tunnel") == os.getpid(), "进程还在就不许清记录（清了会再拉起一个）"


def test_list_三种状态分得清(tmp_path: Path, monkeypatch) -> None:
    """一行读清三种情形：本命令拉起的在跑 / 端口被别人的占着 / 没起。

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
    assert "被占用" in tunnel, tunnel
    assert "不是本命令拉起的" in tunnel, tunnel
    assert "在跑" not in tunnel, f"没有 pid 记录就不许说我们在跑：{tunnel}"
    assert str(os.getpid()) not in tunnel
    assert "没起" in llama, llama
    assert "在跑" in mine and f"pid {os.getpid()}" in mine, mine

