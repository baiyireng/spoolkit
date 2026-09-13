"""`spool config`：看得到生效值，也看得到它从哪来。"""

import argparse
from pathlib import Path

import pytest

from spoolkit import settings
from spoolkit.cli.app import main
from spoolkit.cli.commands.config import config_command
from spoolkit.cli.runtime import resolve_provider_args


@pytest.fixture()
def config_file(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "config.toml"
    monkeypatch.setenv("AGENTS_DEV_CONFIG", str(path))
    for name in ("PROVIDER", "MODEL", "BASE_URL", "PROXY", "SCRIPT"):
        monkeypatch.delenv(f"AGENTS_DEV_{name}", raising=False)
    return path


def _args(**kwargs) -> argparse.Namespace:
    base = {"set": [], "reset": [], "show_path": False}
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_设置之后能读回(config_file: Path, capsys) -> None:
    assert config_command(_args(set=["provider=llamacpp"])) == 0
    assert settings.load()["provider"] == "llamacpp"
    out = capsys.readouterr().out
    assert "llamacpp" in out
    assert "配置文件" in out


def test_键名写错就报错而不是静默(config_file: Path, capsys) -> None:
    assert config_command(_args(set=["providerr=llamacpp"])) == 2
    assert "没有这个键" in capsys.readouterr().err
    assert settings.load() == {}


def test_reset_清掉一项(config_file: Path) -> None:
    settings.save({"provider": "llamacpp", "model": "x"})
    assert config_command(_args(reset=["provider"])) == 0
    assert settings.load() == {"model": "x"}


def test_只打印路径(config_file: Path, capsys) -> None:
    assert config_command(_args(show_path=True)) == 0
    assert str(config_file) in capsys.readouterr().out


def test_命令行盖过配置(config_file: Path) -> None:
    settings.save({"provider": "llamacpp"})
    args = argparse.Namespace(provider="fake", model="", base_url="", proxy="", script="")
    assert resolve_provider_args(args)["provider"] == "fake"


def test_配置生效时来源写清楚(config_file: Path) -> None:
    settings.save({"provider": "llamacpp", "base_url": "http://127.0.0.1:9999"})
    args = argparse.Namespace(provider="", model="", base_url="", proxy="", script="")
    values = resolve_provider_args(args)
    assert values["provider"] == "llamacpp"
    assert "配置文件" in values["base_url_source"]


def test_解析过的值再解析一次不串来源(config_file: Path) -> None:
    """main() 会把生效值写回 args，provider_gateway 再解析一次。

    第二次看到的是"非空的命令行值"，来源就变成了"命令行"——
    显示错了来源比不显示更坏。
    """
    settings.save({"base_url": "http://127.0.0.1:9999"})
    args = argparse.Namespace(provider="", model="", base_url="", proxy="", script="")
    first = resolve_provider_args(args)
    for key, value in first.items():
        setattr(args, key, value)
    second = resolve_provider_args(args)
    assert second["base_url"] == "http://127.0.0.1:9999"
    assert "配置文件" in second["base_url_source"]


def test_入口把_config_挂上了() -> None:
    assert main(["config", "--path"]) == 0
