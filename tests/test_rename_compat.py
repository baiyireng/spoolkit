"""改名（`agents-dev` → `spoolkit`）不许把人机器上已有的东西弄坏。

这几条是**验收条件**，不是锦上添花：旧环境变量、旧配置位置、旧诊断密钥，
凡是用户机器上已经存在的，都要继续工作。改名是一次性的动作，代价不该由
已经配好的人承担——他一个字都不该改。
"""

from pathlib import Path

from spoolkit import diagnosis, settings


def test_旧的配置环境变量还认(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SPOOLKIT_CONFIG", raising=False)
    monkeypatch.setenv("AGENTS_DEV_CONFIG", str(tmp_path / "config.toml"))
    assert settings.config_path() == tmp_path / "config.toml"


def test_新位置没文件时读旧位置那份(monkeypatch, tmp_path: Path) -> None:
    new = tmp_path / "new" / "config.toml"
    old = tmp_path / "old" / "config.toml"
    old.parent.mkdir(parents=True)
    old.write_text('provider = "gemini"\n', encoding="utf-8")
    monkeypatch.delenv("SPOOLKIT_CONFIG", raising=False)
    monkeypatch.delenv("AGENTS_DEV_CONFIG", raising=False)
    monkeypatch.setattr(settings, "config_path", lambda: new)
    monkeypatch.setattr(settings, "legacy_config_path", lambda: old)

    assert settings.load() == {"provider": "gemini"}
    # 来源要说的是**实际读到的那份**，否则人照着提示去找会扑空
    assert str(old) in settings.resolve("provider")[1]


def test_新位置有文件就不再看旧的(monkeypatch, tmp_path: Path) -> None:
    new = tmp_path / "new" / "config.toml"
    old = tmp_path / "old" / "config.toml"
    for path, provider in ((new, "llamacpp"), (old, "gemini")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'provider = "{provider}"\n', encoding="utf-8")
    monkeypatch.delenv("SPOOLKIT_CONFIG", raising=False)
    monkeypatch.delenv("AGENTS_DEV_CONFIG", raising=False)
    monkeypatch.setattr(settings, "config_path", lambda: new)
    monkeypatch.setattr(settings, "legacy_config_path", lambda: old)

    assert settings.load() == {"provider": "llamacpp"}


def test_旧环境变量还被认出来而且报的是真名(monkeypatch) -> None:
    """来源名字必须报**他实际设的那个**——报新前缀等于让他去改一个没设的变量。"""
    monkeypatch.delenv("SPOOLKIT_PROVIDER", raising=False)
    monkeypatch.setenv("AGENTS_DEV_PROVIDER", "llamacpp")
    value, source = settings.resolve("provider")
    assert value == "llamacpp"
    assert "AGENTS_DEV_PROVIDER" in source


def test_新环境变量优先于旧(monkeypatch) -> None:
    monkeypatch.setenv("AGENTS_DEV_PROVIDER", "llamacpp")
    monkeypatch.setenv("SPOOLKIT_PROVIDER", "gemini")
    assert settings.resolve("provider") == ("gemini", "环境变量 SPOOLKIT_PROVIDER")


def test_旧诊断密钥位置还认(monkeypatch, tmp_path: Path) -> None:
    """换位置就不认旧密钥 = 已经签出去的报告全变成"来源无法验证"。"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("SPOOLKIT_DIAGNOSIS_KEY_PATH", raising=False)
    monkeypatch.delenv("AGENTS_DEV_DIAGNOSIS_KEY_PATH", raising=False)
    legacy = tmp_path / ".agents-dev" / "diagnosis.key"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"deadbeef")

    assert diagnosis.key_path() == legacy
    assert diagnosis.load_key() == b"deadbeef"


def test_新诊断密钥存在时优先用它(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("SPOOLKIT_DIAGNOSIS_KEY_PATH", raising=False)
    monkeypatch.delenv("AGENTS_DEV_DIAGNOSIS_KEY_PATH", raising=False)
    for name, payload in ((".agents-dev", b"old"), (".spoolkit", b"new")):
        path = tmp_path / name / "diagnosis.key"
        path.parent.mkdir(parents=True)
        path.write_bytes(payload)

    assert diagnosis.load_key() == b"new"
