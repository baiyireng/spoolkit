"""凭据从哪来：环境变量 → 项目 `.env`，并且要说出是哪一个。

这条是真实需求逼出来的：用户把 `QQ_AppID` / `QQ_AppSecret` 写进了项目根的
`.env`（那里本来就是放密钥的地方），而适配器原先只认进程环境变量——于是
"我明明填了"变成一句没法排查的抱怨。
"""

from pathlib import Path

from agents_dev.bridge import credentials


def _env_file(root: Path, text: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".env").write_text(text, encoding="utf-8")


def test_从_env_里读(tmp_path: Path, monkeypatch) -> None:
    for name in credentials.QQ_APP_ID + credentials.QQ_SECRET:
        monkeypatch.delenv(name, raising=False)
    _env_file(tmp_path, "QQ_AppID=102000000\nQQ_AppSecret=abc123\n")

    value, source = credentials.find(tmp_path, *credentials.QQ_APP_ID)

    assert value == "102000000"
    assert ".env" in source and "QQ_AppID" in source


def test_环境变量优先于_env(tmp_path: Path, monkeypatch) -> None:
    _env_file(tmp_path, "QQ_AppID=from-file\n")
    monkeypatch.setenv("AGENTS_DEV_QQ_APPID", "from-env")

    value, source = credentials.find(tmp_path, *credentials.QQ_APP_ID)

    assert value == "from-env"
    assert "环境变量" in source


def test_认常见的几种拼写(tmp_path: Path, monkeypatch) -> None:
    """用户已经按自己的习惯写下了，让人去改拼写是最没必要的摩擦。"""
    for name in credentials.QQ_APP_ID:
        monkeypatch.delenv(name, raising=False)
    _env_file(tmp_path, "QQ_APPID=102111\n")
    assert credentials.find(tmp_path, *credentials.QQ_APP_ID)[0] == "102111"


def test_找不到时说实话(tmp_path: Path, monkeypatch) -> None:
    for name in credentials.QQ_SECRET:
        monkeypatch.delenv(name, raising=False)
    value, source = credentials.find(tmp_path, *credentials.QQ_SECRET)
    assert value == ""
    assert "没找到" in source


def test_没有工作区也能用(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTS_DEV_QQ_APPID", "x")
    assert credentials.find(None, *credentials.QQ_APP_ID)[0] == "x"


def test_日志里不回显完整密钥() -> None:
    assert credentials.mask("102000000") == "102***00"
    assert credentials.mask("short") == "*****"
    assert credentials.mask("") == "（空）"
