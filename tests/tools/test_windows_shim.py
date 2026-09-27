"""Windows 上 `.cmd` 垫片的换算：`node` 不该因为"它是垫片"而报"找不到命令"。

为什么单独一个文件：这条修的是**归因错误**——`test_js_verify` 在本机红了两批，
一直被当成"沙箱环境债（找不到 node）"，而 node 明明装着：它是
`~\.dsh\preset-runtime\bin\node.cmd`（nvm-for-windows / volta / pnpm 装的 node 也是这种垫片），
而 `subprocess` 在 Windows 上只按 `PATH` 找 `.exe`/`.com`，`.cmd` 不经过 `cmd.exe` 起不来。

边界同时收紧：经 `cmd.exe` 等于让 `& | ^ < > %` 重新变成元字符，而本模块自述
"不经过 shell"，所以 ① 只对 `node` 这么做，② `node --check` 的目标路径里不许出现那些字符。
"""

import os
from pathlib import Path

import pytest

from spoolkit.tools.exec import resolve_argv, validate_command

WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="只有 Windows 需要这层换算")


@WINDOWS_ONLY
def test_node垫片被包成_cmd_c(tmp_path: Path, monkeypatch) -> None:
    """本机的 node 就是 `.cmd` 垫片：换算后必须是 `cmd /c <垫片> …`，否则 spawn 必失败。"""
    import shutil

    shim = tmp_path / "node.cmd"
    shim.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: str(shim))

    got = resolve_argv(["node", "--check", "a.js"], tmp_path)

    assert got == ["cmd", "/c", str(shim), "--check", "a.js"]


@WINDOWS_ONLY
def test_node是真exe时不包cmd(tmp_path: Path, monkeypatch) -> None:
    """`.exe` 直接起就行——多包一层 `cmd` 只会多一处引号与元字符的坑。"""
    exe = tmp_path / "node.exe"
    exe.write_text("", encoding="utf-8")
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: str(exe))

    assert resolve_argv(["node", "--check", "a.js"], tmp_path) == [
        "node",
        "--check",
        "a.js",
    ]


def test_node_check的目标路径不许带shell元字符() -> None:
    """包装成 `cmd /c` 之后，这些字符会重新变成元字符——必须在白名单这一层就挡住。"""
    for bad in ("a&b.js", "a|b.js", "a^b.js", "a<b.js", "a>b.js", "a%b.js"):
        reason = validate_command(["node", "--check", bad])
        assert reason is not None, f"{bad} 应当被拒"
        assert "元字符" in reason

    assert validate_command(["node", "--check", "a.js"]) is None


def test_别的命令不因为垫片而被包cmd(tmp_path: Path, monkeypatch) -> None:
    """这条边界只给 node 开：其它命令找不到就是找不到，不顺手扩大。"""
    shim = tmp_path / "pnpm.cmd"
    shim.write_text("@echo off\n", encoding="utf-8")
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: str(shim))

    assert resolve_argv(["pnpm", "test"], tmp_path) == ["pnpm", "test"]
