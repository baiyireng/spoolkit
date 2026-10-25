"""Windows 上 `.cmd`/`.bat` 垫片的换算：能起，但不许经它偷渡 shell 元字符。

为什么单独一个文件：这条修的是**归因错误**——`test_js_verify` 在本机红了两批，
一直被当成"沙箱环境债（找不到 node）"，而 node 明明装着：它是
`~\\.dsh\\preset-runtime\\bin\\node.cmd`（nvm-for-windows / volta / pnpm 装的 node
也是这种垫片），而 `subprocess` 在 Windows 上只按 `PATH` 找 `.exe`/`.com`，
`.cmd` 不经过 `cmd.exe` 起不来。

一开始只给 `node` 开了这条路。后来发现 `pnpm`、`ruff.cmd` 是同一类东西、撞的是
同一个报错，于是**推广到任意解析到 `.cmd`/`.bat` 的命令**——能起，但两条边界
一起立起来：

1. 参数**逐字**传：仍然是 argv 数组，引用交给 `subprocess.list2cmdline`
   （`shell=False` 时它就是这条命令的引用实现），这里不做字符串拼接；
2. 参数里出现 `& | < > ^ %` 或双引号就**明确拒绝并给人话**——不转义、不猜，
   因为经 `cmd.exe` 转出来的结果没法保证，猜错就是把参数悄悄改了。
"""

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from spoolkit.tools.exec import (
    CmdShimRejected,
    resolve_argv,
    run_once,
    validate_command,
)

WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="只有 Windows 需要这层换算")


def _fake_cmd(tmp_path: Path, name: str) -> Path:
    shim = tmp_path / name
    shim.write_text("@echo off\n", encoding="utf-8")
    return shim


@WINDOWS_ONLY
def test_node垫片被包成_cmd_c(tmp_path: Path, monkeypatch) -> None:
    """本机的 node 就是 `.cmd` 垫片：换算后必须是 `cmd /c call <垫片> …`，否则 spawn 必失败。

    `call` 不是装饰：`cmd /c "<带空格的路径>" …` 会命中 cmd 的引号剥离规则，
    把路径从空格处切两半（见 `resolve_argv` 的说明）。
    """
    shim = _fake_cmd(tmp_path, "node.cmd")
    monkeypatch.setattr(shutil, "which", lambda name: str(shim))

    got = resolve_argv(["node", "--check", "a.js"], tmp_path)

    assert got == ["cmd", "/c", "call", str(shim), "--check", "a.js"]


@WINDOWS_ONLY
def test_node是真exe时不包cmd(tmp_path: Path, monkeypatch) -> None:
    """`.exe` 直接起就行——多包一层 `cmd` 只会多一处引号与元字符的坑。"""
    exe = tmp_path / "node.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: str(exe))

    assert resolve_argv(["node", "--check", "a.js"], tmp_path) == [
        "node",
        "--check",
        "a.js",
    ]


@WINDOWS_ONLY
def test_任意命令的垫片都被包成_cmd_c(tmp_path: Path, monkeypatch) -> None:
    """推广之后这一条对 `pnpm` / `ruff` 一样成立：判据是"解析到的后缀"，
    不挑程序名。改回"只给 node 开"这里就红。"""
    pnpm = _fake_cmd(tmp_path, "pnpm.cmd")
    monkeypatch.setattr(shutil, "which", lambda name: str(pnpm))
    assert resolve_argv(["pnpm", "test"], tmp_path) == [
        "cmd",
        "/c",
        "call",
        str(pnpm),
        "test",
    ]

    ruff = _fake_cmd(tmp_path, "ruff.bat")
    monkeypatch.setattr(shutil, "which", lambda name: str(ruff))
    assert resolve_argv(["ruff", "check", "src"], tmp_path) == [
        "cmd",
        "/c",
        "call",
        str(ruff),
        "check",
        "src",
    ]


@WINDOWS_ONLY
def test_exe不包cmd(tmp_path: Path, monkeypatch) -> None:
    """与程序名无关：`.exe` / `.com` 一律不包（`cmd` 只在垫片上出现）。"""
    for name in ("ruff.exe", "uv.com"):
        real = tmp_path / name
        real.write_text("", encoding="utf-8")
        monkeypatch.setattr(shutil, "which", lambda n, real=real: str(real))
        assert resolve_argv([name.split(".")[0], "--version"], tmp_path) == [
            name.split(".")[0],
            "--version",
        ]


def test_node_check的目标路径不许带shell元字符() -> None:
    """包装成 `cmd /c` 之后，这些字符会重新变成元字符——必须在白名单这一层就挡住。"""
    for bad in ("a&b.js", "a|b.js", "a^b.js", "a<b.js", "a>b.js", "a%b.js"):
        reason = validate_command(["node", "--check", bad])
        assert reason is not None, f"{bad} 应当被拒"
        assert "元字符" in reason

    assert validate_command(["node", "--check", "a.js"]) is None


@WINDOWS_ONLY
def test_垫片加元字符的参数被明确拒绝(tmp_path: Path, monkeypatch) -> None:
    """不转义、不猜：拒绝的同时说清是哪一项、为什么、怎么办。"""
    shim = _fake_cmd(tmp_path, "pnpm.cmd")
    monkeypatch.setattr(shutil, "which", lambda name: str(shim))

    for bad in ("a&b", "a|b", "a<b", "a>b", "a^b", "a%b", 'a"b', "&& whoami"):
        with pytest.raises(CmdShimRejected) as raised:
            resolve_argv(["pnpm", "run", bad], tmp_path)
        message = str(raised.value)
        assert "cmd" in message
        assert repr(bad) in message, message
        assert "怎么办" in message

    # 干净参数照旧能过
    assert resolve_argv(["pnpm", "run", "build"], tmp_path)[:3] == ["cmd", "/c", "call"]


@WINDOWS_ONLY
def test_带元字符的垫片命令给人话而不是拼进命令行(tmp_path: Path, monkeypatch) -> None:
    """走真实工具路径：`cmd /c` 那句话**没有**被拼出来，模型拿到的是人话。

    这条同时钉住"拒绝发生在申请授权之前"——它不该先弹一次批准再拒绝
    （`需要用户批准` 是权限措辞，而这里不是权限问题）。
    """
    shim = _fake_cmd(tmp_path, "pnpm.cmd")
    monkeypatch.setattr(shutil, "which", lambda name: str(shim))

    asked: list[list[str]] = []

    def approver(argv, reason):
        asked.append(list(argv))
        return "once"

    result = run_once(
        tmp_path,
        ["pnpm", "run", "build", "&&", "whoami"],
        approver=approver,
        grants=None,
    )

    assert result.ok is False
    assert "cmd.exe" in result.content
    assert "&&" in result.content
    assert "需要用户批准" not in result.content
    assert asked == [], "注定起不来的命令不该先去打扰人"


# --- ① 参数逐字到达：临时 `.cmd` 垫片把收到的参数写成 JSON -------------------


def _make_arg_capture_shim(shim_dir: Path) -> Path:
    """造一个真实可跑的 `.cmd` 垫片：把 `%*` 交给 python，让它把 argv 落成 JSON。

    为什么垫片里再起一个 python：`.cmd` 自己 `echo` 会把中文按控制台代码页写出去，
    读回来就不是原字符了——那样测的成了编码，而不是"参数有没有被切开"。
    """
    capture = shim_dir / "capture.py"
    capture.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "here = Path(__file__).resolve().parent\n"
        "(here / 'args.json').write_text(\n"
        "    json.dumps(sys.argv[1:], ensure_ascii=False), encoding='utf-8')\n",
        encoding="utf-8",
    )
    shim = shim_dir / "argcap.cmd"
    shim.write_text(
        "@echo off\r\n"
        f'"{sys.executable}" "%~dp0capture.py" %*\r\n',
        encoding="utf-8",
    )
    return shim


@WINDOWS_ONLY
def test_参数经cmd逐字到达(tmp_path: Path, monkeypatch) -> None:
    """空格、中文、中文带空格的路径，一个字符都不能变。

    证伪：把 `resolve_argv` 里的推广去掉（只给 node 开），`argcap` 直接 spawn 会报
    `找不到命令`，这条红；把参数拼成一条字符串（而不是 argv 数组）也会红。
    """
    shim_dir = tmp_path / "shim 目录"
    shim_dir.mkdir()
    _make_arg_capture_shim(shim_dir)
    monkeypatch.setenv("PATH", str(shim_dir) + os.pathsep + os.environ["PATH"])

    # 中文 + 空格的相对路径：既是"中文路径"，也是"参数里有空格"
    (tmp_path / "中文 目录").mkdir()
    (tmp_path / "中文 目录" / "文件.txt").write_text("x", encoding="utf-8")
    wanted = ["参数 带空格", "中文 目录/文件.txt", "普通参数", "--flag=值"]

    result = run_once(
        tmp_path,
        ["argcap", *wanted],
        approver=lambda argv, reason: "once",
    )

    assert result.ok is True, result.content
    captured = json.loads((shim_dir / "args.json").read_text(encoding="utf-8"))
    assert captured == wanted
    # 回显的仍是模型写的那份命令（换算后的 `cmd /c <垫片>` 不露给它）
    assert "$ argcap 参数 带空格" in result.content


@WINDOWS_ONLY
def test_不在白名单的垫片命令也能跑起来(tmp_path: Path, monkeypatch) -> None:
    """这条推广的实际价值：`pnpm`/`ruff` 这类不在白名单的命令，批准之后真能跑。"""
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    _make_arg_capture_shim(shim_dir)
    (shim_dir / "argcap.cmd").rename(shim_dir / "pnpm.cmd")
    monkeypatch.setenv("PATH", str(shim_dir) + os.pathsep + os.environ["PATH"])

    result = run_once(
        tmp_path,
        ["pnpm", "run", "build"],
        approver=lambda argv, reason: "once",
    )

    assert result.ok is True, result.content
    assert json.loads((shim_dir / "args.json").read_text(encoding="utf-8")) == [
        "run",
        "build",
    ]
