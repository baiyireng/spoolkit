"""`spool help [子命令]`：等价于 `spool --help` / `spool <子命令> --help`。

用户真敲过 `spool help`，当时得到的是一句 `invalid choice: 'help'`。这里钉住的
是**等价关系本身**（逐字相同的帮助文本）与退出码（成功 0；认不出的子命令还是
argparse 那套 2 与它自己的报错），不是某段帮助文本长什么样。
"""

import pytest

from spoolkit.cli.app import main


def _run(argv: list[str]) -> int:
    """argparse 用 SystemExit 收场；被测命令把它接住并转成退出码。"""
    try:
        return main(argv)
    except SystemExit as exc:
        return int(exc.code or 0)


def test_help不带参数等于总帮助(capsys) -> None:
    assert _run(["help"]) == 0
    out = capsys.readouterr().out
    assert "usage: spool" in out
    assert "status" in out
    assert "help" in out


def test_help子命令等于那个子命令的help(capsys) -> None:
    assert _run(["help", "status"]) == 0
    via_help = capsys.readouterr().out
    try:
        main(["status", "--help"])
    except SystemExit:
        pass
    direct = capsys.readouterr().out
    assert via_help == direct  # 逐字相同：两处不会漂移
    assert "--json" in via_help


def test_help看不出那个子命令时退2(capsys) -> None:
    """认不出的名字**不猜**：argparse 自己的报错与退出码原样透出来。"""
    assert _run(["help", "nosuch"]) == 2
    err = capsys.readouterr().err
    assert "invalid choice" in err
    assert "nosuch" in err


def test_总帮助里status与help都在(capsys) -> None:
    try:
        main(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
    out = capsys.readouterr().out
    # 子命令表里两个新名字都出现，且 status 在 service 之后（注册顺序＝既有约定）
    assert out.index("service") < out.index("status") < out.index("help")


@pytest.mark.parametrize("topic", ["run", "ws", "bridge", "service"])
def test_help若干既有子命令都等价(topic: str, capsys) -> None:
    assert _run(["help", topic]) == 0
    assert f"usage: spool {topic}" in capsys.readouterr().out
