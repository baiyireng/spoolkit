"""用户配置里的 `[[service]]`：本机外接配方，不进仓库。"""

from pathlib import Path

from spoolkit import settings


def test_没写配方就是空表(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(tmp_path / "config.toml"))
    assert settings.load_services() == []


def test_读得出名字命令和探活(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(path))
    path.write_text(
        "\n".join(
            [
                "provider = \"llamacpp\"",
                "[[service]]",
                'name = "llama"',
                'command = ["llama-server", "-m", "model.gguf", "--port", "8080"]',
                'health = "http://127.0.0.1:8080/props"',
                "[[service]]",
                'name = "tunnel"',
                "command = []",
                'health = "tcp:127.0.0.1:1080"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    items = settings.load_services()
    assert [item.name for item in items] == ["llama", "tunnel"]
    assert items[0].command[0] == "llama-server"
    assert "model.gguf" in items[0].command
    assert items[0].health.startswith("http://")
    assert items[1].command == ()
    assert items[1].health.startswith("tcp:")


def test_没名字的段丢掉(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(path))
    path.write_text(
        '[[service]]\ncommand = ["x"]\nhealth = "tcp:127.0.0.1:1"\n',
        encoding="utf-8",
    )
    assert settings.load_services() == []


def test_改配置键不会把配方表抹掉(tmp_path: Path, monkeypatch) -> None:
    """`spool config --set` 走 save()。配方和 [[mcp]] 必须还在。"""
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(path))
    path.write_text(
        "\n".join(
            [
                'provider = "fake"',
                "[[mcp]]",
                'name = "filesystem"',
                'command = "npx"',
                'args = ["-y", "demo"]',
                "[[service]]",
                'name = "tunnel"',
                'health = "tcp:127.0.0.1:1080"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    settings.save({"provider": "llamacpp", "base_url": "http://127.0.0.1:8080"})
    assert settings.load()["provider"] == "llamacpp"
    assert settings.load_mcp_servers()[0].name == "filesystem"
    assert settings.load_services()[0].name == "tunnel"
    assert settings.load_services()[0].health == "tcp:127.0.0.1:1080"
