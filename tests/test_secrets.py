"""密钥从哪来：**环境变量 → 工作区 `.env` → 用户级 `.env`**。

这一批补的是用户级那一层（`%APPDATA%\\spoolkit\\.env`）：通道、网页壳、MCP 都是
**机器级**服务，"你站在哪个目录"不该决定它读哪份密钥。

真实事故（2026-09-27）：在 `C:\\Users\\32615` 敲
`spool bridge --channel qqbot …`，报"缺少 AppID 与 AppSecret"、并建议把密钥写进
`C:\\Users\\32615\\.env`——而凭据在 `D:\\workSpace\\agentWorkSpace\\.env` 里。
根因是缺省的 `root` 取了 cwd。`test_cwd_不再参与密钥解析` 就是这条的回归用例。
"""

from pathlib import Path

from spoolkit import settings
from spoolkit.bridge import credentials

QQ = credentials.QQ_APP_ID


def _env_file(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _clear_qq(monkeypatch) -> None:
    for name in credentials.QQ_APP_ID + credentials.QQ_SECRET:
        monkeypatch.delenv(name, raising=False)


def test_用户级_env_在任何目录都读得到(tmp_path: Path, monkeypatch) -> None:
    _clear_qq(monkeypatch)
    user_env = _env_file(tmp_path / "roaming" / ".env", "QQ_AppID=102000000\n")
    monkeypatch.setenv("SPOOLKIT_ENV_FILE", str(user_env))

    value, source = settings.resolve_secret(QQ, workspace=None)

    assert value == "102000000"
    assert "用户级" in source and str(user_env) in source


def test_工作区那一份盖住用户级(tmp_path: Path, monkeypatch) -> None:
    """"更具体的赢"：项目里写的那份优先于个人默认那份。"""
    _clear_qq(monkeypatch)
    user_env = _env_file(tmp_path / "roaming" / ".env", "QQ_AppID=from-user\n")
    monkeypatch.setenv("SPOOLKIT_ENV_FILE", str(user_env))
    workspace = tmp_path / "ws"
    _env_file(workspace / ".env", "QQ_AppID=from-workspace\n")

    value, source = settings.resolve_secret(QQ, workspace=workspace)

    assert value == "from-workspace"
    assert "工作区" in source


def test_环境变量盖住两份文件(tmp_path: Path, monkeypatch) -> None:
    _clear_qq(monkeypatch)
    user_env = _env_file(tmp_path / "roaming" / ".env", "QQ_AppID=from-user\n")
    monkeypatch.setenv("SPOOLKIT_ENV_FILE", str(user_env))
    workspace = tmp_path / "ws"
    _env_file(workspace / ".env", "QQ_AppID=from-workspace\n")
    monkeypatch.setenv("AGENTS_DEV_QQ_APPID", "from-env")

    value, source = settings.resolve_secret(QQ, workspace=workspace)

    assert value == "from-env"
    assert "环境变量" in source


def test_别名先给的先赢(tmp_path: Path, monkeypatch) -> None:
    """规范名（新前缀）优先于旧名与用户自留的拼写。"""
    _clear_qq(monkeypatch)
    monkeypatch.setenv("SPOOLKIT_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("QQ_AppID", "plain")
    monkeypatch.setenv("SPOOLKIT_QQ_APPID", "prefixed")

    assert settings.resolve_secret(QQ, workspace=None)[0] == "prefixed"


def test_cwd_不再参与密钥解析(tmp_path: Path, monkeypatch) -> None:
    """回归：站在哪个目录不该改变读哪份密钥。

    `workspace=None`（机器级服务）时只认用户级那一份；当前目录里放着 `.env`
    也不算数——这正是 2026-09-27 那次事故的形状。
    """
    _clear_qq(monkeypatch)
    monkeypatch.setenv("SPOOLKIT_ENV_FILE", str(tmp_path / "missing.env"))
    elsewhere = tmp_path / "elsewhere"
    _env_file(elsewhere / ".env", "QQ_AppID=in-cwd\n")
    monkeypatch.chdir(elsewhere)

    value, source = settings.resolve_secret(QQ, workspace=None)

    assert value == ""
    assert "没找到" in source


def test_找不到时说出找过哪些位置且不印值(tmp_path: Path, monkeypatch) -> None:
    _clear_qq(monkeypatch)
    secret = "s3cr3t-value-should-never-show"
    user_env = _env_file(
        tmp_path / "roaming" / ".env", f"QQ_AppSecret={secret}\nOTHER=x\n"
    )
    monkeypatch.setenv("SPOOLKIT_ENV_FILE", str(user_env))

    text = settings.describe_secret_search(QQ, workspace=None)

    assert str(user_env) in text           # 看过哪个文件要说出来
    assert "QQ_AppSecret" in text          # 文件里有哪些**键**也要说
    assert secret not in text              # 值一个字符都不许出现


def test_描述里没有文件时说没有(tmp_path: Path, monkeypatch) -> None:
    _clear_qq(monkeypatch)
    monkeypatch.setenv("SPOOLKIT_ENV_FILE", str(tmp_path / "nope" / ".env"))

    text = settings.describe_secret_search(QQ, workspace=tmp_path / "ws")

    assert "不存在" in text
    assert "工作区" in text and "用户级" in text


def test_通道凭据走同一条链(tmp_path: Path, monkeypatch) -> None:
    _clear_qq(monkeypatch)
    user_env = _env_file(tmp_path / "roaming" / ".env", "QQ_AppID=102000000\n")
    monkeypatch.setenv("SPOOLKIT_ENV_FILE", str(user_env))

    value, source = credentials.find(None, *QQ)

    assert value == "102000000"
    assert f"用户级 {user_env} 里的 QQ_AppID" == source


def test_网页搜索的_key_也认用户级(tmp_path: Path, monkeypatch) -> None:
    from spoolkit.cli import runtime

    for name in ("TAVILY_API_KEY", "SPOOLKIT_TAVILY_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    user_env = _env_file(tmp_path / "roaming" / ".env", "TAVILY_API_KEY=tv-user\n")
    monkeypatch.setenv("SPOOLKIT_ENV_FILE", str(user_env))

    assert runtime._tavily_api_key(None) == "tv-user"


def test_模型密钥也认用户级(tmp_path: Path, monkeypatch) -> None:
    from spoolkit.llm.providers import ProviderConfig, _resolve_key

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    user_env = _env_file(tmp_path / "roaming" / ".env", "GEMINI_API_KEY=g-user\n")
    monkeypatch.setenv("SPOOLKIT_ENV_FILE", str(user_env))

    config = ProviderConfig(project_root=tmp_path / "ws")

    assert _resolve_key(config) == "g-user"


def test_显式传入仍然最高(tmp_path: Path, monkeypatch) -> None:
    from spoolkit.llm.providers import ProviderConfig, _resolve_key

    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    config = ProviderConfig(project_root=tmp_path, api_key="explicit")

    assert _resolve_key(config) == "explicit"
