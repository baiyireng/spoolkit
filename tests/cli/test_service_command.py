"""`spool service`：按本机配方拉起 / 停掉 / 列出。不写死某台机器。"""

import argparse
import sys
import time
from pathlib import Path

from spoolkit.cli.app import main
from spoolkit.cli.commands.service import service_command
from spoolkit.services import pid_alive, read_pid, start_service, stop_service


def _write_recipe(path: Path, *, name: str, command: list[str], health: str = "") -> None:
    lines = ["[[service]]", f'name = "{name}"']
    inner = ", ".join('"' + part.replace("\\", "\\\\").replace('"', '\\"') + '"' for part in command)
    lines.append(f"command = [{inner}]")
    if health:
        lines.append(f'health = "{health}"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
