"""外挂工具怎么挂进主循环（以及怎么不挂）。"""

import sys
from pathlib import Path

import pytest

from spoolkit.cli import runtime
from spoolkit.cli.runtime import LoopWiring, assemble_loop
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.edit import PendingChanges
from spoolkit.tools.types import ToolCall

STUB = '''
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    if message.get("id") is None:
        continue
    method = message.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "stub", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "now", "description": "报当前时间",
                             "inputSchema": {"type": "object", "properties": {}}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "现在是 12:00"}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}),
          flush=True)
'''


@pytest.fixture()
def clean_mcp():
    """外挂连接是进程级缓存，测试之间必须清干净。"""
    runtime._close_mcp_clients()
    yield
    runtime._close_mcp_clients()


def _configure(tmp_path: Path, monkeypatch, command: str | None = None) -> None:
    stub = tmp_path / "stub.py"
    stub.write_text(STUB, encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text(
        'provider = "fake"\n'
        "\n[[mcp]]\n"
        'name = "clock"\n'
        f"command = '{command or sys.executable}'\n"
        f"args = ['{stub}']\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(config))


def _loop(tmp_path: Path, mcp: bool = True):
    tokenizer = OfflineTokenCounter()
    return assemble_loop(
        tmp_path,
        FakeModel(script=[], tokenizer=tokenizer),
        config=Config(project_root=tmp_path, context_window=4096),
        wiring=LoopWiring(pending=PendingChanges(tmp_path), mcp=mcp),
    )


def test_配了外挂就挂进注册表(tmp_path: Path, monkeypatch, clean_mcp) -> None:
    _configure(tmp_path, monkeypatch)
    loop = _loop(tmp_path)
    assert "mcp__clock__now" in loop.registry.names()
    result = loop.registry.invoke(ToolCall("mcp__clock__now", {}))
    assert result.ok is True
    assert "12:00" in result.content


def test_no_mcp_时不挂(tmp_path: Path, monkeypatch, clean_mcp) -> None:
    _configure(tmp_path, monkeypatch)
    loop = _loop(tmp_path, mcp=False)
    assert not [name for name in loop.registry.names() if name.startswith("mcp__")]


def test_没配就是空操作(tmp_path: Path, monkeypatch, clean_mcp) -> None:
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(tmp_path / "missing.toml"))
    loop = _loop(tmp_path)
    assert not [name for name in loop.registry.names() if name.startswith("mcp__")]


def test_连不上时跳过并说出来(tmp_path: Path, monkeypatch, clean_mcp, capsys) -> None:
    """悄悄降级的结果是模型找不到工具、开始自己造轮子，而人以为配好了。"""
    _configure(tmp_path, monkeypatch, command="definitely-not-a-command-xyz")
    loop = _loop(tmp_path)
    assert not [name for name in loop.registry.names() if name.startswith("mcp__")]
    out = capsys.readouterr().out
    assert "连不上" in out
