"""`spool env`：用户级 `.env` 的位置、键名、写入、从工作区搬。

这一批的目标是"机器级服务在任意目录都能起"，而卡点常常不是命令短不短，而是
**密钥该放哪一层**：通道、网页壳、MCP 读的都是用户级那一层
（`%APPDATA%\\spoolkit\\.env`），而人手上的凭据通常已经在某个工作区的 `.env` 里。
`import` 就是为这一步准备的：一条命令搬过来，不必把密钥重新打一遍（也就不进 shell 历史）。

规矩：**不回显值**（只印遮罩形状）、**不动别人的行**（同键替换，注释原样保留）。
"""

import argparse
from pathlib import Path

from spoolkit import settings
from spoolkit.cli.app import main
from spoolkit.cli.commands.env import env_command
from spoolkit.envfile import load_env_file


def _user_env(tmp_path: Path, monkeypatch, text: str = "") -> Path:
    """把用户级 `.env` 指到临时文件（conftest 已经指了，这里显式写清是哪个）。"""
    path = tmp_path / "roaming" / ".env"
    path.parent.mkdir(parents=True, exist_ok=True)
    if text:
        path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("SPOOLKIT_ENV_FILE", str(path))
    return path


def _args(**over) -> argparse.Namespace:
    base = dict(action="list", items=[], source="", only=[])
    base.update(over)
    return argparse.Namespace(**base)


# --- 看 ---


def test_path_只印路径(tmp_path: Path, monkeypatch, capsys) -> None:
    path = _user_env(tmp_path, monkeypatch)

    code = env_command(_args(action="path"))

    assert code == 0
    assert capsys.readouterr().out.strip() == str(path)


def test_list_不存在时说清怎么建(tmp_path: Path, monkeypatch, capsys) -> None:
    path = _user_env(tmp_path, monkeypatch)

    code = env_command(_args(action="list"))
    out = capsys.readouterr().out

    assert code == 0
    assert str(path) in out and "不存在" in out
    assert "spool env set" in out and "spool env import" in out


def test_list_只印键名与形状_不印值(tmp_path: Path, monkeypatch, capsys) -> None:
    secret = "s3cr3t-should-never-be-printed"
    _user_env(tmp_path, monkeypatch, f"QQ_AppID=102000000\nQQ_AppSecret={secret}\n")

    code = env_command(_args(action="list"))
    out = capsys.readouterr().out

    assert code == 0
    assert "QQ_AppID" in out and "QQ_AppSecret" in out
    assert "102***00" in out  # 形状能认出是哪一份
    assert secret not in out  # 值一个字符都不许出现


# --- 写 ---


def test_set_建文件并只印形状(tmp_path: Path, monkeypatch, capsys) -> None:
    path = _user_env(tmp_path, monkeypatch)

    code = env_command(_args(action="set", items=["QQ_AppID=102000000"]))
    out = capsys.readouterr().out

    assert code == 0
    assert load_env_file(path) == {"QQ_AppID": "102000000"}
    assert "102***00" in out and "102000000" not in out


def test_set_同键替换且不动别人的行(tmp_path: Path, monkeypatch) -> None:
    """整份重写的代价是手写注释被抹掉，然后两份配置开始漂移。"""
    path = _user_env(
        tmp_path,
        monkeypatch,
        "# 这台机器的密钥\nQQ_AppID=old\n\n# 下面这条是给搜索用的\nTAVILY_API_KEY=tv\n",
    )

    code = env_command(_args(action="set", items=["QQ_AppID=new"]))

    text = path.read_text(encoding="utf-8")
    assert code == 0
    assert "# 这台机器的密钥" in text and "# 下面这条是给搜索用的" in text
    assert text.count("QQ_AppID") == 1
    assert load_env_file(path)["QQ_AppID"] == "new"
    assert load_env_file(path)["TAVILY_API_KEY"] == "tv"


def test_set_形状不对时点名并给用法(tmp_path: Path, monkeypatch, capsys) -> None:
    _user_env(tmp_path, monkeypatch)

    code = env_command(_args(action="set", items=["QQ_AppID", "=x"]))
    err = capsys.readouterr().err

    assert code == 2
    assert "QQ_AppID" in err and "用法" in err


def test_set_什么都没给时给用法(tmp_path: Path, monkeypatch, capsys) -> None:
    _user_env(tmp_path, monkeypatch)

    assert env_command(_args(action="set", items=[])) == 2
    assert "用法" in capsys.readouterr().err


# --- 搬 ---


def test_import_把工作区的凭据搬到用户级(tmp_path: Path, monkeypatch, capsys) -> None:
    """正解：一条命令把已经在工作区里的凭据升级成机器级，不必重打、不进 shell 历史。"""
    path = _user_env(tmp_path, monkeypatch)
    source = tmp_path / "ws" / ".env"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "QQ_AppID=102000000\nQQ_AppSecret=sec-value\nTAVILY_API_KEY=tv\n",
        encoding="utf-8",
    )

    code = env_command(
        _args(action="import", source=str(source), only=["QQ_AppID", "QQ_AppSecret"])
    )
    out = capsys.readouterr().out

    assert code == 0
    assert load_env_file(path) == {"QQ_AppID": "102000000", "QQ_AppSecret": "sec-value"}
    assert source.is_file()  # **来源文件没动**
    assert "sec-value" not in out  # 值不印
    assert "QQ_AppSecret" in out


def test_import_来源里缺的键要点名(tmp_path: Path, monkeypatch, capsys) -> None:
    path = _user_env(tmp_path, monkeypatch)
    source = tmp_path / "ws" / ".env"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("QQ_AppID=102000000\n", encoding="utf-8")

    code = env_command(_args(action="import", source=str(source), only=["QQ_AppSecret"]))
    err = capsys.readouterr().err

    assert code == 1
    assert "QQ_AppSecret" in err
    assert not path.is_file()  # 一个都没搬，就别建出个空文件来


def test_import_来源不存在时说清是哪个路径(tmp_path: Path, monkeypatch, capsys) -> None:
    _user_env(tmp_path, monkeypatch)

    code = env_command(_args(action="import", source=str(tmp_path / "nope" / ".env")))
    err = capsys.readouterr().err

    assert code == 1
    assert "nope" in err


def test_import_没给来源时给用法(tmp_path: Path, monkeypatch, capsys) -> None:
    _user_env(tmp_path, monkeypatch)

    assert env_command(_args(action="import", source="")) == 2
    assert "用法" in capsys.readouterr().err


# --- 接线：命令真的能敲（走 main，不直接调函数） ---


def test_命令行能敲通(tmp_path: Path, monkeypatch, capsys) -> None:
    path = _user_env(tmp_path, monkeypatch)

    assert main(["env", "set", "QQ_AppID=102000000"]) == 0
    assert "102***00" in capsys.readouterr().out

    assert main(["env", "path"]) == 0
    assert capsys.readouterr().out.strip() == str(path)

    assert main(["env", "--from", str(path), "import", "--only", "QQ_AppID"]) == 0
    assert load_env_file(path)["QQ_AppID"] == "102000000"


def test_命令行_命令表里有env() -> None:
    """防"实现了但没注册"：`spool env` 必须出现在帮助里。"""
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.suppress(SystemExit), contextlib.redirect_stdout(buf):
        main(["--help"])
    assert "env" in buf.getvalue()
