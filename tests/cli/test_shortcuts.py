"""短命令 `spool web` / `spool qq`：等价关系、默认值来自哪、以及"没被改"的那些。

两条契约：

1. **等价**——`spool web` 就是 `hub` 不带通道，`spool qq` 就是
   `bridge --channel qqbot …`。参数面共用同一份定义，行为一步不差。
2. **默认值有三层**——命令行 > 环境变量 > 用户配置 `[channel.qqbot]` > 内置默认。

外加同样重要的一条：**`serve` / `hub` / `bridge` 的既有行为一个字节都没变**。
短命令是"多给的一层默认值"，不是"把长命令也改了"。

每条用例都能证伪：把 `_fill_from_channel` 去掉（那就是长命令的行为）、把 argparse
的默认值从 `None` 改回具体值（配置就永远盖不过命令行）、或者让短命令去用 cwd，
对应那条就变红。
"""

import argparse
import sys
from pathlib import Path

import pytest

from spoolkit import settings
from spoolkit.cli import app as cli_app
from spoolkit.cli.app import main
from spoolkit.cli.commands import shortcuts

WORKSPACE_ENV = "SPOOLKIT_ENV_FILE"


# --- 夹具 -------------------------------------------------------------------


@pytest.fixture()
def config_file(tmp_path: Path, monkeypatch) -> Path:
    """独立的用户配置（conftest 已隔离全局那份，这里只为了拿到路径写东西）。"""
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(path))
    return path


@pytest.fixture()
def user_env(tmp_path: Path, monkeypatch) -> Path:
    """用户级 `.env`（密钥那一层）。`spool qq` 只认这一份，不认 cwd。"""
    path = tmp_path / "user" / ".env"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(WORKSPACE_ENV, str(path))
    return path


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _parse(command: str, argv: list[str]) -> argparse.Namespace:
    """走**真的**那份 argparse 定义：默认值错了这里就会露出来。

    这里**只**挂 subparsers，不额外加一个名叫 `command` 的位置参数：加了的话
    那个位置参数会先吃掉命令行上的 `web`/`qq`，于是子解析器一个参数都轮不到
    ——症状是所有用例都拿不到 `web` / `bridge_proxy`（`AttributeError`），
    或者非空参数被当成"多余的"（`SystemExit: 2`）。而子解析器是
    `required=False` 的，坏掉时**不报错、静默跳过**，所以这个夹具必须与
    `main()` 里的那份定义逐字同形。
    """
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    getattr(cli_app, f"_add_{command}_command")(sub)
    args = parser.parse_args([command, *argv])
    assert getattr(args, "command", None) == command, "子解析器没接上，参数面全是空的"
    # `main()` 会做的两件事，这里照做，好让命令看到与真实运行一致的值。
    args.root = str(getattr(args, "root", "") or "")
    args.session = str(getattr(args, "session", "") or "")
    return args


@pytest.fixture()
def fake_qq(monkeypatch) -> dict:
    """记账的假 QQ 通道：**不连网关**（长连接、也不该在单测里真起）。"""
    seen: dict = {}

    class _FakeQQ:
        def __init__(self, **kwargs) -> None:
            seen.update(kwargs)
            self.app_id = kwargs.get("app_id") or ""
            self.secret = kwargs.get("secret") or ""

        def start(self) -> None:
            seen.setdefault("started", 0)
            seen["started"] += 1

        def access_token(self) -> str:
            return "T-TOKEN"

        def gateway_url(self) -> str:
            return "wss://example.invalid/websocket"

        def close(self) -> None:
            pass

    monkeypatch.setattr("spoolkit.bridge.qqbot.QQBotChannel", _FakeQQ)
    return seen


@pytest.fixture()
def credentials(user_env: Path) -> Path:
    """凭据放在**用户级** `.env`：这是 `spool qq` 唯一该看的那一份。"""
    return _write(user_env, "QQ_AppID=102000000\nQQ_AppSecret=secret-value\n")


# --- ① 两条命令出现在 --help 里，而且在任意目录能起 --------------------------


def test_总帮助里有这两条(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "web" in out
    assert "qq" in out


def test_web帮助说的是机器级网页壳(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["web", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "机器级" in out
    assert "任意目录" in out
    assert "--no-web" in out
    assert "--port" in out


def test_qq帮助说的是按用户配置起通道(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["qq", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "channel.qqbot" in out
    assert "--check" in out
    assert "--bridge-proxy" in out


def test_web在任意目录能起_不钉cwd(monkeypatch, tmp_path: Path) -> None:
    """不给 `--root` 就是机器级：**不解析目标**，站哪儿都一样（设计 §3.2）。

    证伪法：让 `web_command` 去用 cwd（或让 argparse 的 `--root` 默认值非空），
    这个用例立刻变红——它会报"当前目录不是工作区"或者钉住 cwd。
    """
    elsewhere = tmp_path / "任意目录"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    seen: dict = {}
    monkeypatch.setattr(
        "spoolkit.cli.commands.hub.serve",
        lambda root, **kwargs: seen.update(root=root, kwargs=kwargs),
    )

    assert main(["web", "--port", "0"]) == 0

    assert seen["root"] is None, "机器级网页壳不该在启动时就钉一个工作区"
    assert seen["kwargs"]["pinned"] is False


def test_web把参数交给hub那一套(monkeypatch, config_file: Path) -> None:
    """参数逐字落到 `hub_command` 手上：短命令不许自己再解释一遍。"""
    seen: dict = {}
    monkeypatch.setattr(
        "spoolkit.cli.commands.shortcuts.hub_command",
        lambda args: seen.update(args=args) or 0,
    )

    assert shortcuts.web_command(_parse("web", ["--port", "9999", "--policy", "ask"])) == 0

    assert seen["args"].port == 9999
    assert seen["args"].policy == "ask"
    assert seen["args"].channel == "", "spool web 的契约就是不带通道"


def test_qq不传通道也认死qqbot(monkeypatch, config_file: Path, fake_qq) -> None:
    seen: dict = {}
    monkeypatch.setattr(
        "spoolkit.cli.commands.shortcuts.bridge_command",
        lambda args: seen.update(args=args) or 0,
    )

    assert shortcuts.qq_command(_parse("qq", [])) == 0

    assert seen["args"].channel == "qqbot"


# --- ② qq 的默认值来自用户配置；命令行覆盖优先 -------------------------------


def test_终端的参数跟着用户配置变(monkeypatch, config_file: Path) -> None:
    """改用户配置 → 传给桥子进程的那串参数跟着变。

    这条是整个 `[channel.qqbot]` 小节存在的理由：那六个参数**没有一个属于
    "这一次"**，它们是这台机器上的默认值。
    """
    from spoolkit.cli.commands.hub import bridge_child_argv

    _write(
        config_file,
        '[channel.qqbot]\nproxy = "socks5://127.0.0.1:1080"\npolicy = "ask"\n'
        'scope = "**"\ntimeout = 240\nweb = false\n',
    )
    captured: dict = {}
    monkeypatch.setattr(
        "spoolkit.cli.commands.shortcuts.bridge_command",
        lambda args: captured.update(args=args) or 0,
    )

    assert shortcuts.qq_command(_parse("qq", [])) == 0

    args = captured["args"]
    assert args.bridge_proxy == "socks5://127.0.0.1:1080"
    assert args.policy == "ask"
    assert args.scope == "**"
    assert args.timeout == 240.0
    assert args.web is False

    argv = bridge_child_argv(args)
    # 这三个在"起子进程那条路"上也要带过去（`hub --channel` 走的就是这条路）。
    for flag, value in (
        ("--bridge-proxy", "socks5://127.0.0.1:1080"),
        ("--policy", "ask"),
        ("--scope", "**"),
    ):
        assert argv[argv.index(flag) + 1] == value
    assert "--no-web" in argv and "--web" not in argv
    # `--timeout` **不在这串里**，这是既有行为、本批不动：`bridge_child_argv`
    # 从来不发它，hub 的参数面上也没有 `--timeout`。`spool qq` 的桥是**本进程**
    # 里起的，直接读上面那五个生效值（`bridge_command` 拿 `args.timeout` 建
    # `AgentRunner`），所以 timeout 由 `args.timeout == 240.0` 那一条钉住——
    # 证伪法：去掉 `_fill_from_channel`，它会掉回内置默认 900.0。


def test_命令行覆盖用户配置(monkeypatch, config_file: Path) -> None:
    _write(
        config_file,
        '[channel.qqbot]\nproxy = "socks5://配置"\npolicy = "ask"\ntimeout = 240\n',
    )
    captured: dict = {}
    monkeypatch.setattr(
        "spoolkit.cli.commands.shortcuts.bridge_command",
        lambda args: captured.update(args=args) or 0,
    )

    assert (
        shortcuts.qq_command(
            _parse(
                "qq",
                [
                    "--bridge-proxy",
                    "socks5://命令行",
                    "--policy",
                    "auto",
                    "--timeout",
                    "60",
                    "--web",
                ],
            )
        )
        == 0
    )

    args = captured["args"]
    assert args.bridge_proxy == "socks5://命令行"
    assert args.policy == "auto"
    assert args.timeout == 60.0
    assert args.web is True


def test_空串也是命令行给的(monkeypatch, config_file: Path) -> None:
    """`--scope ""` 是"不许自动落盘"，配置里那份盖不过它。

    这一条证伪的是"用真假判有没有给"：那样写的话配置里的 `scope` 会反过来
    生效，而人明明在命令行上写了。
    """
    _write(config_file, '[channel.qqbot]\nscope = "**"\n')
    captured: dict = {}
    monkeypatch.setattr(
        "spoolkit.cli.commands.shortcuts.bridge_command",
        lambda args: captured.update(args=args) or 0,
    )

    assert shortcuts.qq_command(_parse("qq", ["--scope", ""])) == 0

    assert captured["args"].scope == ""


def test_环境变量夹在命令行与用户配置中间(
    monkeypatch, config_file: Path, credentials: Path
) -> None:
    _write(config_file, '[channel.qqbot]\npolicy = "ask"\n')
    monkeypatch.setenv("SPOOLKIT_CHANNEL_QQBOT_POLICY", "deny")
    captured: dict = {}
    monkeypatch.setattr(
        "spoolkit.cli.commands.shortcuts.bridge_command",
        lambda args: captured.update(args=args) or 0,
    )

    assert shortcuts.qq_command(_parse("qq", [])) == 0
    assert captured["args"].policy == "deny", "环境变量要压过用户配置"

    assert shortcuts.qq_command(_parse("qq", ["--policy", "auto"])) == 0
    assert captured["args"].policy == "auto", "命令行要压过环境变量"


def test_没有这一节时与长命令同口径(
    monkeypatch, config_file: Path, credentials: Path
) -> None:
    """**兼容**：没写 `[channel.qqbot]` 时，短命令的默认值就是内置默认。"""
    captured: dict = {}
    monkeypatch.setattr(
        "spoolkit.cli.commands.shortcuts.bridge_command",
        lambda args: captured.update(args=args) or 0,
    )

    assert shortcuts.qq_command(_parse("qq", [])) == 0

    args = captured["args"]
    assert args.timeout == 900.0
    assert args.policy == ""
    assert args.scope == ""
    assert args.bridge_proxy == ""
    assert args.web is True


def test_认不出的键在起手时就印出来(capsys, monkeypatch, config_file: Path) -> None:
    """写错一个字母的症状是"设置了却没生效"——所以起手第一屏就要说。"""
    _write(config_file, '[channel.qqbot]\nproxi = "socks5://127.0.0.1:1080"\n')
    monkeypatch.setattr(
        "spoolkit.cli.commands.shortcuts.bridge_command", lambda args: 0
    )

    assert shortcuts.qq_command(_parse("qq", [])) == 0

    err = capsys.readouterr().err
    assert "channel.qqbot" in err
    assert "proxi" in err
    assert "不认识" in err


def test_起手那行说得出每个值是从哪来的(
    capsys, monkeypatch, config_file: Path
) -> None:
    """这几个值生效在别的进程里，出问题时唯一能回答"读了哪一份"的就是这一行。

    `root` 写在这一节里也**不许**出现在这一行：这行写的是"本次生效"，而短命令
    不读配置里的 `root`（工作区在 `main()` 那一步就定死了）。把没生效的键印成
    生效值，正是这套东西要消灭的那种病——"终端说用它、实际跑另一条"。
    证伪法：把那行的键表从 `_CHANNEL_OPTION_KEYS` 换回 `settings.CHANNEL_KEYS`，
    下面 `root=` 那一条立刻变红。
    """
    _write(
        config_file,
        '[channel.qqbot]\ntimeout = 240\nroot = "D:\\\\某处"\n',
    )
    monkeypatch.setattr(
        "spoolkit.cli.commands.shortcuts.bridge_command", lambda args: 0
    )

    assert shortcuts.qq_command(_parse("qq", [])) == 0

    err = capsys.readouterr().err
    assert "本次生效" in err
    assert "240.0" in err
    assert "[channel.qqbot]" in err
    assert "内置默认" in err
    assert "root=" not in err, "没生效的键不许印进「本次生效」那一行"


# --- ③ 凭据从用户级 .env 来，cwd 里的那份不算数 ------------------------------


def test_凭据从用户级env来_且报告里说得出(
    capsys, monkeypatch, credentials: Path, fake_qq
) -> None:
    """用户报的真实场景：在 `C:\\Users\\32615` 直接敲 `spool qq` 就能起来。"""
    assert main(["qq", "--check"]) == 0

    out = capsys.readouterr().out
    assert str(credentials) in out
    assert "QQ_AppID" in out
    assert "secret-value" not in out, "密钥不进终端"


def test_cwd里的env不算数(monkeypatch, tmp_path: Path, user_env: Path, fake_qq) -> None:
    """**机器级入口不认 cwd**：脚下的 `.env` 既读不到、也盖不住用户级那份。

    真实事故就是这个形状：在 `C:\\Users\\32615` 起桥，它去找
    `C:\\Users\\32615\\.env`，而凭据在别处。
    """
    here = tmp_path / "cwd"
    _write(here / ".env", "QQ_AppID=999999999\nQQ_AppSecret=cwd-secret\n")
    _write(user_env, "QQ_AppID=102000000\nQQ_AppSecret=user-secret\n")
    monkeypatch.chdir(here)

    assert main(["qq", "--check"]) == 0

    assert fake_qq["app_id"] == "102000000", "读到的是 cwd 那份"
    assert fake_qq["secret"] == "user-secret"
    assert fake_qq["root"] is None, "cwd 被当成了工作区"


def test_缺凭据时在通道那一步就停住(monkeypatch, user_env: Path, tmp_path: Path) -> None:
    """没有凭据 → 建通道那一步就报错并指到 `spool env`，**不连网关**。

    这一条走的是**真的** `bridge_command`：要测的就是"桥自己在建通道那一步停住"。
    所以打的是 `QQBotChannel.start`（起长连接那一下），**不是**把整条
    `bridge_command` 换掉——换掉等于把被测的那一步删了，那条路无论对不对都"通过"。
    凭据检查一旦去掉，`start()` 会被调到，这条立刻变红。
    """
    here = tmp_path / "cwd"
    here.mkdir()
    monkeypatch.chdir(here)
    monkeypatch.setattr(
        "spoolkit.bridge.qqbot.QQBotChannel.start",
        lambda self: (_ for _ in ()).throw(AssertionError("缺凭据时不该起长连接")),
    )

    with pytest.raises(SystemExit) as exc:
        main(["qq"])

    message = str(exc.value)
    assert "AppID" in message and "AppSecret" in message
    assert "spool env set QQ_AppID" in message, "缺凭据的下一步要能照着敲"
    assert "找过这些位置" in message
    assert str(user_env) in message


def test_check把用户配置里的代理传下去(
    monkeypatch, config_file: Path, credentials: Path, fake_qq
) -> None:
    """`[channel.qqbot].proxy` 是**通道出网**那条路（`--bridge-proxy`），

    不传下去的后果是"检查通过、真跑失败"——QQ 平台有 IP 白名单，直连和借主机
    出去是两条不同的路。
    """
    _write(config_file, '[channel.qqbot]\nproxy = "socks5://127.0.0.1:1080"\n')

    assert main(["qq", "--check"]) == 0

    assert fake_qq["proxy"] == "socks5://127.0.0.1:1080"


def test_check走的是真正要跑的那条路(
    monkeypatch, config_file: Path, credentials: Path, fake_qq
) -> None:
    """`--check` 的参数必须与真起通道时一致，否则检查工具在骗人。"""
    _write(config_file, '[channel.qqbot]\nsandbox = true\n')  # 认不出的键，不该生效
    captured: dict = {}

    def fake_bridge(args):
        captured["args"] = args
        return 0

    monkeypatch.setattr("spoolkit.cli.commands.shortcuts.bridge_command", fake_bridge)

    assert main(["qq", "--check"]) == 0
    assert captured["args"].check is True
    assert captured["args"].sandbox is False


# --- ④ serve / hub / bridge 的既有行为没被改 --------------------------------


def test_web不给no_web时仍然是开(capsys, monkeypatch, tmp_path: Path) -> None:
    """短命令的 `--web` 默认值从 `True` 改成了 `None`（"用户没表态"）。

    这一条钉住"没表态 = 开"：改错了的话，网页壳与桥会在没人要求的情况下
    变成 `--no-web`——而那是**静默**少掉一整块能力。
    """
    monkeypatch.setattr(
        "spoolkit.cli.commands.hub.serve", lambda root, **kwargs: None
    )

    assert main(["web", "--port", "0"]) == 0


def test_不给no_web时子进程拿到web(monkeypatch, config_file: Path) -> None:
    from spoolkit.cli.commands.hub import extra_from_serve_args

    captured: dict = {}
    monkeypatch.setattr(
        "spoolkit.cli.commands.shortcuts.hub_command",
        lambda args: captured.update(args=args) or 0,
    )

    assert shortcuts.web_command(_parse("web", [])) == 0

    extra = extra_from_serve_args(captured["args"])
    assert "--web" in extra and "--no-web" not in extra


def test_web_no_web是显式的(capsys, monkeypatch, tmp_path: Path) -> None:
    from spoolkit.cli.commands.hub import extra_from_serve_args

    captured: dict = {}
    monkeypatch.setattr(
        "spoolkit.cli.commands.shortcuts.hub_command",
        lambda args: captured.update(args=args) or 0,
    )

    assert shortcuts.web_command(_parse("web", ["--no-web"])) == 0

    assert captured["args"].web is False
    assert "--no-web" in extra_from_serve_args(captured["args"])


def test_serve的web默认值没变() -> None:
    """`serve` / `hub` 的参数面一个字没动：短命令是**多给的一层**，不是改长命令。"""
    assert _parse("serve", []).web is True
    assert _parse("hub", []).web is True
    assert _parse("qq", []).web is None, "短命令要多一个'用户没表态'"


def test_serve在任意目录仍然不钉目标(monkeypatch, tmp_path: Path) -> None:
    elsewhere = tmp_path / "任意目录"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    seen: dict = {}
    monkeypatch.setattr(
        "spoolkit.cli.commands.serve.serve",
        lambda root, **kwargs: seen.update(root=root, kwargs=kwargs),
    )

    assert main(["serve", "--port", "0"]) == 0

    assert seen["root"] is None
    assert seen["kwargs"]["pinned"] is False


def test_bridge不读channel配置(monkeypatch, config_file: Path, credentials: Path) -> None:
    """`[channel.*]` **只**给短命令当默认值：`spool bridge` 的参数默认值今天
    是什么样，今天还是什么样（改长命令等于改既有行为，那是另一件事）。

    打桩打在 `spoolkit.cli.app.bridge_command` 上，**不是** `...commands.bridge`：
    `main()` 建解析器时把命令函数绑进了 `args.func`，那个绑定取自
    `app` 这个命名空间的全局名。打在命令模块上改不到它，于是这条用例会真的
    起一条桥（`bridge --channel qqbot` 等消息），**一挂就是一整轮**。
    """
    _write(config_file, '[channel.qqbot]\npolicy = "auto"\ntimeout = 240\nweb = false\n')
    captured: dict = {}
    monkeypatch.setattr(
        "spoolkit.cli.app.bridge_command",
        lambda args: captured.update(args=args) or 0,
    )

    assert main(["bridge", "--channel", "qqbot"]) == 0

    args = captured["args"]
    assert args.policy == "", "长命令不读 [channel.*]"
    assert args.timeout == 900.0
    assert args.web is True


def test_hub不读channel配置(monkeypatch, config_file: Path) -> None:
    """同上：`hub --channel qqbot` 的参数默认值不变（用户配置只服务短命令）。

    打桩同样打在 `spoolkit.cli.app.hub_command`（理由见上一条）：打在命令模块上
    会漏过去，而漏过去的后果是**真的把网页壳起来**——它绑端口、等请求，不返回。
    """
    _write(config_file, '[channel.qqbot]\npolicy = "auto"\nweb = false\n')
    captured: dict = {}
    monkeypatch.setattr(
        "spoolkit.cli.app.hub_command",
        lambda args: captured.update(args=args) or 0,
    )

    assert main(["hub", "--channel", "qqbot"]) == 0

    assert captured["args"].policy == ""
    assert captured["args"].web is True


def test_root给了就是钉住(monkeypatch, tmp_path: Path, credentials: Path) -> None:
    """`spool qq --root X` 仍然钉住 X（向后兼容的那一半）。"""
    workspace = tmp_path / "某个工作区"
    (workspace / ".agent").mkdir(parents=True)
    captured: dict = {}
    monkeypatch.setattr(
        "spoolkit.cli.commands.shortcuts.bridge_command",
        lambda args: captured.update(args=args) or 0,
    )

    assert main(["qq", "--root", str(workspace)]) == 0

    assert Path(captured["args"].root) == workspace.resolve()


def test_qq不真起长连接(monkeypatch, credentials: Path, fake_qq) -> None:
    """`--check` 那条路**不起通道**（不起 websocket、不跑桥循环）。

    真假都在 `bridge --check` 的实现里；这里只确认短命令没有绕开它。
    """
    assert main(["qq", "--check"]) == 0

    assert "started" not in fake_qq, "起手那一步把长连接拉起来了"


def test_qq_no_setup不弹向导(monkeypatch, credentials: Path, fake_qq) -> None:
    """机器级短命令不该在别人的脚本里弹出配置向导（会挂住自动化）。"""
    called: list = []
    monkeypatch.setattr(
        "spoolkit.cli.app.onboarding.interactive", lambda: True
    )
    monkeypatch.setattr("spoolkit.cli.app.onboarding.needs_setup", lambda argv=None: True)
    monkeypatch.setattr(
        "spoolkit.cli.app.onboarding.run_wizard",
        lambda root, **kwargs: called.append(root),
    )

    assert main(["qq", "--check", "--no-setup"]) == 0

    assert called == []


def test_短命令在非交互环境下不弹向导(monkeypatch, credentials: Path, fake_qq) -> None:
    """不是终端时不问问题（CI / 脚本 / 被别的程序调起）：那会挂住别人的自动化。

    这一条与上一条是两回事：上一条说的是 `--no-setup`（人显式要求），这一条
    说的是**环境本身没有终端**（`onboarding.interactive()` 为假）。
    """
    called: list = []
    monkeypatch.setattr("spoolkit.cli.app.onboarding.interactive", lambda: False)
    monkeypatch.setattr("spoolkit.cli.app.onboarding.needs_setup", lambda argv=None: True)
    monkeypatch.setattr(
        "spoolkit.cli.app.onboarding.run_wizard",
        lambda root, **kwargs: called.append(root),
    )

    assert main(["qq", "--check"]) == 0

    assert called == []
