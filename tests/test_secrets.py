"""密钥从哪来：**环境变量 → 工作区 `.env` → 用户级 `.env`**。

这一批补的是用户级那一层（`%APPDATA%\\spoolkit\\.env`）：通道、网页壳、MCP 都是
**机器级**服务，"你站在哪个目录"不该决定它读哪份密钥。

真实事故（2026-09-27）：在 `C:\\Users\\32615` 敲
`spool bridge --channel qqbot …`，报"缺少 AppID 与 AppSecret"、并建议把密钥写进
`C:\\Users\\32615\\.env`——而凭据在 `D:\\workSpace\\agentWorkSpace\\.env` 里。

头一版只改了 `settings` 这一层，被独立评审逮住：**调用方照样把 cwd 当工作区喂进来**
（`bridge.py` 的 `Path(getattr(args, "root", "."))`），于是 `cwd/.env` 反而**盖住**
用户级那份——优先级反转。所以这里有两侧的用例：`settings` 层（解析顺序）与
**调用方**（`_build_channel` / `_check` 不许拿 cwd 顶数）。
"""

import argparse
from pathlib import Path

import pytest

from spoolkit import settings
from spoolkit.bridge import credentials

QQ = credentials.QQ_APP_ID
QQ_ALL = credentials.QQ_APP_ID + credentials.QQ_SECRET


def _env_file(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _clear_qq(monkeypatch) -> None:
    for name in QQ_ALL:
        monkeypatch.delenv(name, raising=False)


def _qq_args(root: str = "", **over) -> argparse.Namespace:
    """`spool bridge --channel qqbot` 那种 Namespace（只给 `_build_channel` 要的字段）。"""
    base = dict(
        channel="qqbot", root=root, appid="", secret="", sandbox=False, bridge_proxy=""
    )
    base.update(over)
    return argparse.Namespace(**base)


# --- settings 层：解析顺序 ---


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


def test_机器级不认cwd_里的_env(tmp_path: Path, monkeypatch) -> None:
    """回归：`workspace=None` 时只认用户级那一份，当前目录里的 `.env` 不算数。"""
    _clear_qq(monkeypatch)
    monkeypatch.setenv("SPOOLKIT_ENV_FILE", str(tmp_path / "missing.env"))
    elsewhere = tmp_path / "elsewhere"
    _env_file(elsewhere / ".env", "QQ_AppID=in-cwd\n")
    monkeypatch.chdir(elsewhere)

    value, source = settings.resolve_secret(QQ, workspace=None)

    assert value == ""
    assert "没找到" in source


# --- 调用方：桥不许把 cwd 当工作区 ---


def test_机器级桥不拿cwd当工作区(tmp_path: Path, monkeypatch) -> None:
    """评审 Critical 的回归：`spool bridge` 不给 `--root` 时，cwd 里的 `.env`

    绝不能被当成"工作区那一层"读进来——那会**静默盖住**用户级 `.env`，而缺凭据的
    文案还会推荐人往那个位置写（照做就得到一个压过正确配置的文件）。
    """
    from spoolkit.cli.commands.bridge import _build_channel

    _clear_qq(monkeypatch)
    monkeypatch.setenv("SPOOLKIT_ENV_FILE", str(tmp_path / "no-user.env"))
    dirty = tmp_path / "dirty-cwd"
    _env_file(dirty / ".env", "QQ_AppID=from-cwd\nQQ_AppSecret=from-cwd\n")
    monkeypatch.chdir(dirty)

    with pytest.raises(SystemExit) as exc:  # cwd 那两个**不算**，所以这里是缺凭据
        _build_channel(_qq_args(root=""), lambda *_: None)

    text = str(exc.value)
    assert "QQ 官方机器人需要" in text
    assert str(settings.env_path()) in text  # 文案指向用户级那层
    assert "dirty-cwd" not in text  # 不再推荐往 cwd 写


def test_显式给了root_就认那个工作区的env(tmp_path: Path, monkeypatch) -> None:
    """另一侧：`--root` 显式给了就该被当工作区（工作区那层要参与解析）。"""
    from spoolkit.cli.commands.bridge import _build_channel

    _clear_qq(monkeypatch)
    monkeypatch.setenv("SPOOLKIT_ENV_FILE", str(tmp_path / "no-user.env"))
    workspace = tmp_path / "ws"
    _env_file(workspace / ".env", "QQ_AppID=\n")  # 有键但没值 → 仍算缺凭据

    with pytest.raises(SystemExit) as exc:
        _build_channel(_qq_args(root=str(workspace)), lambda *_: None)

    text = str(exc.value)
    assert str(workspace / ".env") in text
    assert "没有要找的键" in text and "QQ_AppID" in text


def test_check_在机器级下不再崩(tmp_path: Path, monkeypatch, capsys) -> None:
    """评审 Important 的回归：`spool bridge --check` 不给 `--root` 时原是 TypeError。

    而它偏偏是这台机器上**唯一能回答"凭据从哪来"**的命令，机器级又正是它最该
    工作的场合。
    """
    from spoolkit.cli.commands.bridge import _check

    _clear_qq(monkeypatch)
    monkeypatch.setenv("SPOOLKIT_ENV_FILE", str(tmp_path / "no-user.env"))

    code = _check(None, argparse.Namespace(channel="qqbot", sandbox=False, bridge_proxy=""))
    out = capsys.readouterr().out

    assert code == 2
    assert "机器级" in out
    assert "找过这些位置" in out and str(settings.env_path()) in out


# --- 描述："我明明填了" ---


def test_找不到时说出找过哪些位置且不印值(tmp_path: Path, monkeypatch) -> None:
    _clear_qq(monkeypatch)
    secret = "s3cr3t-value-should-never-show"
    user_env = _env_file(
        tmp_path / "roaming" / ".env", f"QQ_AppSecret={secret}\nOTHER=x\n"
    )
    monkeypatch.setenv("SPOOLKIT_ENV_FILE", str(user_env))

    text = settings.describe_secret_search(QQ_ALL, workspace=None)

    assert f"[用户级] {user_env} —— 里面有：QQ_AppSecret" in text
    assert secret not in text  # 值一个字符都不许出现


def test_描述区分不存在_不是文件_解析不出(tmp_path: Path, monkeypatch) -> None:
    """三种"看起来都不是它"的情况要分开说，因为下一步完全不同。"""
    _clear_qq(monkeypatch)
    broken = tmp_path / "broken.env"
    broken.write_bytes(b"\x00\x01\x02not-utf8\xff")
    monkeypatch.setenv("SPOOLKIT_ENV_FILE", str(broken))

    text = settings.describe_secret_search(QQ, workspace=tmp_path / "no-such-ws")

    assert f"[工作区] {tmp_path / 'no-such-ws' / '.env'} —— 不存在" in text
    assert "一行都没解析出来" in text and "字节" in text


def test_描述说得出路径指到了目录(tmp_path: Path, monkeypatch) -> None:
    _clear_qq(monkeypatch)
    a_dir = tmp_path / "a-dir"
    a_dir.mkdir()
    monkeypatch.setenv("SPOOLKIT_ENV_FILE", str(a_dir))

    text = settings.describe_secret_search(QQ, workspace=None)

    assert f"[用户级] {a_dir} —— 不是文件" in text


# --- `.env` 的读法（编码两条坑都来自"我明明填了"） ---


def test_utf8带bom的键名不带bom(tmp_path: Path) -> None:
    """记事本存 UTF-8 常带 BOM；不处理的话键名会变成 `\\ufeffQQ_AppID`。"""
    from spoolkit.envfile import load_env_file

    path = tmp_path / ".env"
    path.write_bytes("QQ_AppID=102\n".encode("utf-8-sig"))

    assert load_env_file(path) == {"QQ_AppID": "102"}


def test_utf16写的env也读得出来(tmp_path: Path) -> None:
    """记事本存"Unicode"就是 UTF-16——那种文件绝不能被说成"空文件"。"""
    from spoolkit.envfile import load_env_file

    path = tmp_path / ".env"
    path.write_bytes("QQ_AppID=102\nQQ_AppSecret=abc\n".encode("utf-16"))

    assert load_env_file(path) == {"QQ_AppID": "102", "QQ_AppSecret": "abc"}


# --- 三个消费者共用同一条链 ---


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
    """护栏（对本 diff 不是回归）：将来有人重排优先级时，显式参数仍该在最前。"""
    from spoolkit.llm.providers import ProviderConfig, _resolve_key

    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    config = ProviderConfig(project_root=tmp_path, api_key="explicit")

    assert _resolve_key(config) == "explicit"


# --- 用户的字面路径（事故现场） ---


def test_事故现场_在别处起桥也能拿到用户级凭据(tmp_path: Path, monkeypatch) -> None:
    """把当天的现场跑一遍：cwd 是家目录（没有 `.env`），凭据在用户级那层。

    这是那个用户真正要的结果——**在任意目录都能起机器级通道**。
    """
    _clear_qq(monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    user_env = _env_file(tmp_path / "roaming" / ".env", "QQ_AppID=102000000\n")
    monkeypatch.setenv("SPOOLKIT_ENV_FILE", str(user_env))
    monkeypatch.chdir(home)

    value, source = settings.resolve_secret(QQ, workspace=None)

    assert value == "102000000"
    assert str(user_env) in source
