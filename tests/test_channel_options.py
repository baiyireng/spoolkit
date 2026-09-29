"""用户配置的 `[channel.<名字>]`：默认值、优先级、说不出口的那些。

这一节存在的理由很具体：起一条通道原先要敲六个参数
（`--channel qqbot --bridge-proxy socks5://… --policy ask --scope "**"
--timeout 240 --web`），而这六个**没有一个属于"这一次"**——它们都是这台机器上的
默认值。所以它们该有地方放，也该有统一的三层优先级：
**命令行 > 环境变量 > 用户配置 > 内置默认**。

两条规矩在这里被钉住，因为它们都是"设置了却没生效"的形状：

1. **认不出的键要说出口**（照 `describe_missing` 的先例）。写错一个字母的症状
   不是报错，而是"我明明配了"；
2. **取值写错同样要说出口**：`policy` 是枚举、`timeout` 要正数——命令行那边
   argparse 挡着，配置这条路原先谁都不看，于是 `policy = "AUTO"` 会静默落到
   "问"那一条路上；
3. **`spool config --set` 不能把这一节写丢**。它是人手写的（`--set` 管不到子表），
   丢了之后 `spool qq` 只是"参数回到默认值"，一句话都不会说。

每条用例都能证伪：把优先级顺序换一下、把 `unknown` 去掉、把 `save()` 里的
`channel` 从"跳过"列表里删掉，对应那条就变红。
"""

from pathlib import Path

import pytest

from spoolkit import settings
from spoolkit.cli.app import main


@pytest.fixture()
def config_file(tmp_path: Path, monkeypatch) -> Path:
    """一份独立的用户配置。conftest 已经隔离过全局那份，这里只是要拿到路径。"""
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(path))
    return path


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- 读取 -------------------------------------------------------------------


def test_没写这一节时全是内置默认(config_file: Path) -> None:
    """**兼容**：老配置里没有 `[channel.*]`，行为与今天一模一样。"""
    values = settings.channel_options("qqbot")

    assert values.options == dict(settings.CHANNEL_KEYS)
    assert values.options["web"] is True
    assert values.options["timeout"] == 900.0, "内置默认要与 bridge --timeout 同口径"
    assert set(values.sources.values()) == {"内置默认"}
    assert values.unknown == ()
    assert values.notes() == ""


def test_写了就读得出来(config_file: Path) -> None:
    _write(
        config_file,
        '[channel.qqbot]\nproxy = "socks5://127.0.0.1:1080"\npolicy = "ask"\n'
        'scope = "**"\ntimeout = 240\nweb = false\n',
    )
    values = settings.channel_options("qqbot")

    assert values.options["proxy"] == "socks5://127.0.0.1:1080"
    assert values.options["policy"] == "ask"
    assert values.options["scope"] == "**"
    assert values.options["timeout"] == 240.0
    assert values.options["web"] is False
    for key in ("proxy", "policy", "scope", "timeout", "web"):
        assert "[channel.qqbot]" in values.source(key), "来源要指到这一节"
        assert str(config_file) in values.source(key), "还要指到那份文件"
    assert values.source("root") == "内置默认"


def test_每个通道各读自己那一节(config_file: Path) -> None:
    """`[channel.a]` 不许漏到 `[channel.b]` 头上——两个通道是可以各配各的。"""
    _write(
        config_file,
        '[channel.qqbot]\npolicy = "ask"\n\n[channel.telegram]\npolicy = "deny"\n',
    )

    assert settings.channel_options("qqbot").options["policy"] == "ask"
    assert settings.channel_options("telegram").options["policy"] == "deny"
    assert settings.channel_options("wecom").options["policy"] == ""
    assert settings.known_channels() == ["qqbot", "telegram"]


@pytest.mark.parametrize("written", ['"很久"', '"abc"'])
def test_值读不成该有的类型要说出来(config_file: Path, written: str) -> None:
    """`timeout = "很久"` / `"abc"` 不能被静默换回默认值——那就是"我明明配了却没生效"。"""
    _write(config_file, f"[channel.qqbot]\ntimeout = {written}\n")

    values = settings.channel_options("qqbot")

    assert values.options["timeout"] == 900.0, "读不成还是要有值，否则桥会崩"
    assert values.issues and "timeout" in values.issues[0]
    assert "读不成数字" in values.issues[0], "要说清是哪一种不对（数字 vs 正数）"
    assert "timeout" in values.notes()


def test_policy只认命令行认的那三个取值(config_file: Path) -> None:
    """`policy` 是**枚举**，配置这条路原先谁都不看它。

    后果不是报错，而是"我明明配了自动，它还在问我"——`settle.py` 只认 `deny` /
    `auto`，其余一律走"问"。命令行那边 argparse 的 `choices` 早就挡住了
    （`spool qq --policy Ask` 直接退出 2），配置文件这条路必须同样说得出口。
    大小写不宽容是**故意**的：命令行只认小写，配置层就不该偷偷认大写——
    悄悄把 `"AUTO"` 改成 `"auto"` 是另一种"我写的东西被改了"。
    """
    for wrong in ("Ask", "AUTO", "Deny"):
        _write(config_file, f'[channel.qqbot]\npolicy = "{wrong}"\n')
        values = settings.channel_options("qqbot")

        assert values.options["policy"] == "", "认不出的取值不许生效，退回下一层"
        assert values.source("policy") == "内置默认"
        notes = values.notes()
        assert "policy" in notes and wrong in notes, "要指出是哪一个值不对"
        assert "ask" in notes and "auto" in notes and "deny" in notes, (
            "顺手把可用的取值列出来，人才知道该写什么"
        )


def test_policy的空串仍然是没表态(config_file: Path) -> None:
    """`policy = ""` 是合法取值：`resolve_policy` 见到假值就去读已保存的策略。

    把它也当成"写错了"，症状是**配置里明写着的一行开始报错**——那就把
    "这一层不做类型判断以外的猜测"这条边界踩坏了。
    """
    _write(config_file, '[channel.qqbot]\npolicy = ""\n')

    values = settings.channel_options("qqbot")

    assert values.options["policy"] == ""
    assert values.issues == ()


def test_timeout不是正数要说出来(config_file: Path) -> None:
    """`timeout = -5` / `timeout = 0`：一轮等待的上界不能是 0 或负数。

    依据在消费方 `bridge/agent_runner.py`：`deadline = now + timeout`，于是
    0 与负数意味着**每一轮当场就到点**，人看到的是"它说超过 -5 秒还没结束，
    先不等了"。同仓 `tools/exec.py` 对命令超时也是拒绝（"必须在 1 到 N 秒之间"）。
    这里沿用同一条口径：退回下一层，并在起手时说出口。
    """
    for wrong in (-5, 0):
        _write(config_file, f"[channel.qqbot]\ntimeout = {wrong}\n")
        values = settings.channel_options("qqbot")

        assert values.options["timeout"] == 900.0, "用不了的值退回内置默认"
        assert values.issues and "timeout" in values.issues[0]
        assert "大于 0" in values.issues[0], "要说清是哪一种不对，别只说'读不成类型'"
        assert "大于 0" in values.notes()


def test_环境变量里的负timeout同样说出口(config_file: Path, monkeypatch) -> None:
    """配置与环境变量走的是同一条判定（`_coerce_channel_value`），别一边查一边不查。"""
    monkeypatch.setenv("SPOOLKIT_CHANNEL_QQBOT_TIMEOUT", "-1")
    _write(config_file, "[channel.qqbot]\ntimeout = 240\n")

    values = settings.channel_options("qqbot")

    assert values.options["timeout"] == 240.0, "坏的那一层跳过，好的那层照用"
    assert values.issues and "环境变量" in values.issues[0]


def test_开关类型写错要说出来(config_file: Path) -> None:
    """`web = "大概"` 这种读不成真假的值，同样不许悄悄换回默认值。"""
    _write(config_file, '[channel.qqbot]\nweb = "大概"\n')

    values = settings.channel_options("qqbot")

    assert values.options["web"] is True, "退回内置默认"
    assert values.issues and "web" in values.issues[0]
    assert "真假" in values.notes()


def test_开关的宽容写法照认不报错(config_file: Path) -> None:
    """`web = "no"` / `"yes"` 认下、**不报错**，这是有意的。

    环境变量那一层永远是字符串（`SPOOLKIT_CHANNEL_QQBOT_WEB=no` 是自然的写法），
    而这两种写法**不会改变"配了什么"**——`yes` 就是"开"、`no` 就是"关"，没有
    "设置了却没生效"的成分。要比命令行更严的只有两处：拿不准的取值（`policy`）
    和用不了的数值（`timeout`）。证伪法：把本地的宽容分支删掉，这条变红。
    """
    _write(config_file, '[channel.qqbot]\nweb = "no"\n')

    values = settings.channel_options("qqbot")

    assert values.options["web"] is False
    assert values.issues == ()

    _write(config_file, '[channel.qqbot]\nweb = "yes"\n')
    assert settings.channel_options("qqbot").options["web"] is True


def test_config_命令里开关按真假印(config_file: Path, capsys) -> None:
    """`spool config` 印的 `web = false` 要与起手那一行**同一个形状**（小写 true/false）。

    两处各印各的，人就会在两个命令之间看到 `False` 与 `false` 两种写法，
    然后开始怀疑哪一边是真的。形状只此一份：`settings.display_channel_value`。
    """
    _write(config_file, "[channel.qqbot]\nweb = false\n")

    assert main(["config"]) == 0

    out = capsys.readouterr().out
    web_line = next(
        line
        for line in out.splitlines()
        if line.strip() and line.strip().split()[0] == "web"
    )
    assert "false" in web_line
    assert "False" not in web_line, "TOML 里写的是 false，印出来也该是 false"


def test_config_命令把短命令不读的键标出来(config_file: Path, capsys) -> None:
    """`root` 印在别的键旁边、形状一模一样，配了它的人会以为生效。

    起手那一行早就把它排除了，`spool config` 这一节却一直把它当生效值印——
    同一份配置，两处说法不一样。只标不删：删了的话配过 root 的人连"我配过"
    都看不见（那比标出来更坏）。
    """
    # 值给得长一点：`root` 的值本来就是路径，比列宽长时必须与来源分得开，
    # 否则印成 `D:\某个很长的工作区名字用户配置 [channel.qqbot] …`。
    _write(
        config_file,
        '[channel.qqbot]\nroot = "D:\\\\某个很长的工作区名字"\npolicy = "ask"\n',
    )

    assert main(["config"]) == 0

    out = capsys.readouterr().out
    root_line = next(
        line for line in out.splitlines() if line.strip().startswith("root")
    )
    # 注记取 `←` 之后那一段：临时目录名里带着用例名，直接断言整行会被路径骗过去。
    note = root_line.partition("←")[2]
    assert "短命令不读" in note, "要让这一行看起来就与生效值那几行不一样"
    assert "--root" in note, "还要说清正确的给法在哪儿"
    assert str(config_file) in root_line, "值本身照印，只是多了一句注记"
    assert " 用户配置 [channel.qqbot]" in root_line, "值与来源之间至少要有一个空格"
    policy_line = next(
        line for line in out.splitlines() if line.strip().startswith("policy")
    )
    assert "←" not in policy_line, "真在生效的键不许被标"


# --- 优先级 -----------------------------------------------------------------


def test_命令行的值到这里为止(config_file: Path, monkeypatch) -> None:
    """命令行给了的键**不再往下看**，哪怕给的是空串。

    空串是一个合法取值（`--scope ""` = 不许自动落盘）。把它当成"没给"，
    配置里那份就会反过来盖住它——而人明明在命令行上写了。
    """
    _write(config_file, '[channel.qqbot]\nscope = "**"\nproxy = "socks5://配置文件"\n')

    values = settings.resolve_channel_options(
        "qqbot", {"scope": "", "proxy": "socks5://命令行"}
    )

    assert values.options["scope"] == ""
    assert values.sources["scope"] == "命令行"
    assert values.options["proxy"] == "socks5://命令行"


def test_环境变量压过用户配置(config_file: Path, monkeypatch) -> None:
    _write(config_file, '[channel.qqbot]\nproxy = "socks5://配置文件"\n')
    monkeypatch.setenv("SPOOLKIT_CHANNEL_QQBOT_PROXY", "socks5://环境变量")

    values = settings.channel_options("qqbot")

    assert values.options["proxy"] == "socks5://环境变量"
    assert values.sources["proxy"] == "环境变量 SPOOLKIT_CHANNEL_QQBOT_PROXY"


def test_旧前缀也认但新的优先(config_file: Path, monkeypatch) -> None:
    """改名兼容：`AGENTS_DEV_CHANNEL_*` 不许因为我们改了个名字就失效。"""
    monkeypatch.setenv("AGENTS_DEV_CHANNEL_QQBOT_SCOPE", "src")
    assert settings.channel_options("qqbot").options["scope"] == "src"
    assert (
        settings.channel_options("qqbot").sources["scope"]
        == "环境变量 AGENTS_DEV_CHANNEL_QQBOT_SCOPE"
    )

    monkeypatch.setenv("SPOOLKIT_CHANNEL_QQBOT_SCOPE", "tests")
    values = settings.channel_options("qqbot")
    assert values.options["scope"] == "tests"
    assert values.sources["scope"] == "环境变量 SPOOLKIT_CHANNEL_QQBOT_SCOPE"


def test_环境变量里的数字与开关要按类型读(config_file: Path, monkeypatch) -> None:
    """环境变量永远是字符串，而 `timeout` 要能当数字用、`web` 要能当真假用。"""
    monkeypatch.setenv("SPOOLKIT_CHANNEL_QQBOT_TIMEOUT", "240")
    monkeypatch.setenv("SPOOLKIT_CHANNEL_QQBOT_WEB", "false")

    values = settings.channel_options("qqbot")

    assert values.options["timeout"] == 240.0
    assert isinstance(values.options["timeout"], float)
    assert values.options["web"] is False


def test_用户配置压过内置默认(config_file: Path) -> None:
    _write(config_file, "[channel.qqbot]\nweb = false\ntimeout = 30\n")
    values = settings.channel_options("qqbot")

    assert values.options["web"] is False
    assert values.options["timeout"] == 30.0
    assert values.options["policy"] == "", "没写的键还是内置默认"
    assert values.source("policy") == "内置默认"


# --- 说不出口的那些 ---------------------------------------------------------


def test_认不出的键要说出口(config_file: Path) -> None:
    """本仓的教训：写错一个字母的症状是"设置了却没生效"，而那种最难查。"""
    _write(config_file, '[channel.qqbot]\nproxi = "socks5://127.0.0.1:1080"\n')

    values = settings.channel_options("qqbot")

    assert values.unknown == ("proxi",)
    notes = values.notes()
    assert "proxi" in notes
    assert "不认识" in notes
    assert "proxy" in notes, "要顺手把可用的键列出来，人才知道差在哪"
    assert values.options["proxy"] == "", "认不出的键不参与生效值"


def test_config_命令要把这一节的毛病印出来(config_file: Path, capsys) -> None:
    """光在库里说出来没用：`spool config` 是人唯一会去敲的那条命令。"""
    _write(config_file, '[channel.qqbot]\nproxi = "x"\n')

    assert main(["config"]) == 0

    out = capsys.readouterr().out
    assert "channel.qqbot" in out
    assert "proxi" in out


def test_config_命令要把这一节的值印出来(config_file: Path, capsys) -> None:
    _write(config_file, '[channel.qqbot]\npolicy = "ask"\ntimeout = 240\n')

    assert main(["config"]) == 0

    out = capsys.readouterr().out
    assert "channel.qqbot" in out
    assert "ask" in out
    assert "240" in out


# --- 保存不能丢 -------------------------------------------------------------


def test_save不丢通道小节(config_file: Path) -> None:
    """`spool config --set provider=…` 一次，通道默认值就整节没了。

    丢的方式还很安静：`spool qq` 只是"参数回到了默认值"，一句话都不会说。
    """
    _write(
        config_file,
        'provider = "llamacpp"\n\n[channel.qqbot]\npolicy = "ask"\n'
        'proxy = "socks5://127.0.0.1:1080"\ntimeout = 240\n',
    )

    settings.save({"provider": "gemini"})

    payload, problem = settings.read_config(config_file)
    assert problem == ""
    assert payload["channel"]["qqbot"]["policy"] == "ask"
    assert payload["channel"]["qqbot"]["timeout"] == 240
    assert payload["provider"] == "gemini"
    # 存回来之后还要读得出来（TOML 的归属很容易写歪，只断言文件里有字不够）
    assert settings.channel_options("qqbot").options["policy"] == "ask"


def test_save不会把通道键写成顶层标量(config_file: Path) -> None:
    """`[channel.x]` 是子表。写成顶层标量（`channel.qqbot = "…"`）会**静默改掉
    后面所有顶层键的归属**——TOML 会把它们全归进那个刚被定义出来的子表，
    也就是把别人的配置搬了家。这是真会改语义的一种坏，所以钉住。"""
    _write(config_file, '[channel.qqbot]\npolicy = "ask"\n')

    settings.save({"provider": "gemini"})

    text = config_file.read_text(encoding="utf-8")
    assert "channel.qqbot =" not in text
    assert "[channel.qqbot]" in text
    payload, _ = settings.read_config(config_file)
    assert payload["provider"] == "gemini", "顶层键不许被吞进子表"


def test_save连不改的通道也一起留着(config_file: Path) -> None:
    _write(
        config_file,
        '[channel.qqbot]\npolicy = "ask"\n\n[channel.telegram]\npolicy = "deny"\n',
    )

    settings.save({"provider": "gemini"})

    assert settings.channel_options("qqbot").options["policy"] == "ask"
    assert settings.channel_options("telegram").options["policy"] == "deny"
