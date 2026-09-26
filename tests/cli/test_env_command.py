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
    """防"实现了但没注册"：帮助里得真有 `env` 这一条。

    断言要精确到"有一行的开头就是 env 且带着它的说明"——裸子串匹配太弱
    （"env" 二字在帮助的别处也可能出现，比如环境变量的说明文字里）。
    """
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.suppress(SystemExit), contextlib.redirect_stdout(buf):
        main(["--help"])
    help_text = buf.getvalue()

    assert any(
        line.strip().startswith("env ") and "用户级" in line
        for line in help_text.splitlines()
    ), help_text


# --- 评审 Important：读得动、一改就炸 / 悄悄打两份 ---


def test_set_目标是utf16时改得动(tmp_path: Path, monkeypatch, capsys) -> None:
    """评审 Important：`list` 能读 UTF-16，`set` 却按 utf-8 读 → 直接崩。

    "看着能用、一改就炸"比干脆不支持更坏。
    """
    path = _user_env(tmp_path, monkeypatch)
    path.write_bytes("QQ_AppID=old\n".encode("utf-16"))

    code = env_command(_args(action="set", items=["QQ_AppID=new"]))

    assert code == 0
    assert path.read_bytes()[:2] in (b"\xff\xfe", b"\xfe\xff")  # 还是 UTF-16
    assert load_env_file(path) == {"QQ_AppID": "new"}


def test_set_目标带bom时同键替换不打两份(tmp_path: Path, monkeypatch) -> None:
    """评审 Important：BOM 让首行键变成 `\\ufeffQQ_AppID` → 判定成"别人的键"→ 追加。

    后果是文件里留着旧密钥、而"同键替换"静默失效。
    """
    path = _user_env(tmp_path, monkeypatch)
    path.write_bytes("QQ_AppID=old\n".encode("utf-8-sig"))

    assert env_command(_args(action="set", items=["QQ_AppID=new"])) == 0

    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    assert raw[:3] == b"\xef\xbb\xbf"  # BOM 还在（原样写回）
    assert text.count("QQ_AppID") == 1
    assert load_env_file(path) == {"QQ_AppID": "new"}


def test_set_保持原有的换行风格(tmp_path: Path, monkeypatch) -> None:
    """CRLF 被整份改写成 LF，在 git 里就是"整个文件都改了"。"""
    path = _user_env(tmp_path, monkeypatch)
    path.write_bytes(b"# note\r\nQQ_AppID=old\r\n")

    assert env_command(_args(action="set", items=["QQ_AppID=new"])) == 0

    raw = path.read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    assert load_env_file(path)["QQ_AppID"] == "new"


def test_set_保留首尾空行(tmp_path: Path, monkeypatch) -> None:
    path = _user_env(tmp_path, monkeypatch)
    path.write_bytes(b"\n\nQQ_AppID=old\n\n")

    assert env_command(_args(action="set", items=["QQ_AppID=new"])) == 0

    text = path.read_text(encoding="utf-8")
    assert text.startswith("\n\n") and text.endswith("\n\n")
    assert load_env_file(path)["QQ_AppID"] == "new"


def test_set_认export写法且留住前缀(tmp_path: Path, monkeypatch) -> None:
    """`export KEY=VALUE` 是最常见的 shell 写法：认它，替换时也要留住 `export `。"""
    path = _user_env(tmp_path, monkeypatch, "export QQ_AppID=old\n")

    assert env_command(_args(action="set", items=["QQ_AppID=new"])) == 0

    text = path.read_text(encoding="utf-8")
    assert text.count("QQ_AppID") == 1
    assert text.strip() == "export QQ_AppID=new"
    assert load_env_file(path) == {"QQ_AppID": "new"}


def test_set_不合形状的参数不回显(tmp_path: Path, monkeypatch, capsys) -> None:
    """评审 Minor，但它是**唯一实测出来的值泄漏路径**：打错的常常就是密钥本身

    （少一个 `=`），原样回显等于把它打进终端、滚动缓冲、截图。
    """
    _user_env(tmp_path, monkeypatch)
    looks_like_secret = "102abcSECRETVALUE"

    code = env_command(_args(action="set", items=[looks_like_secret]))
    captured = capsys.readouterr()

    assert code == 2
    assert looks_like_secret not in captured.out
    assert looks_like_secret not in captured.err
    assert "不是 KEY=VALUE 的形状" in captured.err


def test_set_目标是目录时给人话(tmp_path: Path, monkeypatch, capsys) -> None:
    a_dir = tmp_path / "a-dir"
    a_dir.mkdir()
    monkeypatch.setenv("SPOOLKIT_ENV_FILE", str(a_dir))

    code = env_command(_args(action="set", items=["QQ_AppID=x"]))
    err = capsys.readouterr().err

    assert code == 1
    assert "是个目录" in err and "Traceback" not in err


def test_import_only不能是空的(tmp_path: Path, monkeypatch, capsys) -> None:
    _user_env(tmp_path, monkeypatch)
    source = tmp_path / "ws" / ".env"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("QQ_AppID=102000000\n", encoding="utf-8")

    code = env_command(_args(action="import", source=str(source), only=[""]))
    err = capsys.readouterr().err

    assert code == 2
    assert "--only" in err


def test_import_目标不可读时给人话(tmp_path: Path, monkeypatch, capsys) -> None:
    """目标 `.env` 是二进制（认不出的编码）时，不许把回溯甩给人。"""
    path = _user_env(tmp_path, monkeypatch)
    path.write_bytes(b"\x81\x82\x83\xff\xfe\x00\x01")
    source = tmp_path / "ws" / ".env"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("QQ_AppID=102000000\n", encoding="utf-8")

    code = env_command(_args(action="import", source=str(source)))
    err = capsys.readouterr().err

    assert code == 1
    assert "Traceback" not in err
    assert "文本编码" in err


def test_list_目标是目录时说明白(tmp_path: Path, monkeypatch, capsys) -> None:
    a_dir = tmp_path / "a-dir"
    a_dir.mkdir()
    monkeypatch.setenv("SPOOLKIT_ENV_FILE", str(a_dir))

    code = env_command(_args(action="list"))
    out = capsys.readouterr().out

    assert code == 0
    assert "是个目录" in out
