"""用户级配置：优先级与来源。

规则只有一条，但它必须被钉死：**命令行 > 环境变量 > 配置文件 > 内置默认**。
顺序错了的症状不是报错，而是"我明明设了却没生效"——而这句话会让人开始
怀疑代码，而不是怀疑优先级。
"""

from pathlib import Path

import pytest

from agents_dev import settings


@pytest.fixture()
def config_file(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "config.toml"
    monkeypatch.setenv("AGENTS_DEV_CONFIG", str(path))
    return path


def test_没有配置文件时返回空(config_file: Path) -> None:
    assert settings.load() == {}
    assert settings.resolve("provider") == ("", "未设置")


def test_写进去能读出来(config_file: Path) -> None:
    settings.save({"provider": "llamacpp", "base_url": "http://127.0.0.1:8080"})
    assert settings.load() == {
        "provider": "llamacpp",
        "base_url": "http://127.0.0.1:8080",
    }
    assert config_file.is_file()


def test_只写白名单里的键(config_file: Path) -> None:
    settings.save({"provider": "llamacpp", "乱写的键": "x"})
    assert settings.load() == {"provider": "llamacpp"}


def test_配置文件损坏时不挡启动(config_file: Path) -> None:
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text("这不是 toml = = =", encoding="utf-8")
    assert settings.load() == {}


def test_命令行优先于环境变量与配置(config_file: Path, monkeypatch) -> None:
    settings.save({"provider": "llamacpp"})
    monkeypatch.setenv("AGENTS_DEV_PROVIDER", "gemini")
    assert settings.resolve("provider") == ("gemini", "环境变量 AGENTS_DEV_PROVIDER")
    assert settings.resolve("provider", flag="fake") == ("fake", "命令行")


def test_环境变量优先于配置文件(config_file: Path, monkeypatch) -> None:
    settings.save({"base_url": "http://a"})
    monkeypatch.setenv("AGENTS_DEV_BASE_URL", "http://b")
    value, source = settings.resolve("base_url")
    assert value == "http://b"
    assert "AGENTS_DEV_BASE_URL" in source


def test_来源要说得出文件路径(config_file: Path) -> None:
    settings.save({"provider": "llamacpp"})
    value, source = settings.resolve("provider")
    assert value == "llamacpp"
    assert str(config_file) in source


def test_配置文件不在工作区里() -> None:
    """工作区是要提交、要分享的，不该承载某台机器的默认值。"""
    path = settings.config_path()
    assert "agents-dev" in str(path)
    assert ".agent" not in str(path)
