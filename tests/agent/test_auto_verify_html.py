"""写 HTML → 自动验证 → 结论回灌：这条链在真循环里必须真的验 HTML。

dogfood 现场：写贪吃蛇 HTML 时，系统的验证能力只有 pytest（只认 Python）与
node --check（只认 .js），于是「自动验证」对 HTML 什么都没说，模型自己撞命令，
9 步白费。这一组用例走**真循环**（tests/agent 的既有夹具形状），证明现在
系统自己会把 HTML 的结论顶回去。
"""

import json
import shutil
from pathlib import Path

import pytest

from spoolkit.agent.loop import AgentLoop
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.edit import PendingChanges, write_file_spec
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.verify import make_verifier

# 缺一个 id：getElementById('ghost') 指向不存在的元素。
# 这个缺陷 node --check 看不出来（语法全对），pytest 也看不出来——
# 旧行为在「有 pytest 的项目」里对 HTML 一个字都不说。
BAD_HTML = """<!DOCTYPE html>
<html>
<body>
<canvas id="cv"></canvas>
<script>
const cv = document.getElementById('ghost');
cv.width = 320;
</script>
</body>
</html>
"""

GOOD_HTML = BAD_HTML.replace("'ghost'", "'cv'")


def _turn(thought: str, calls=None, final=None) -> str:
    return json.dumps(
        {
            "thought": thought,
            "tool_calls": calls or [],
            "state": None,
            "done": final is not None,
            "final": final,
        },
        ensure_ascii=False,
    )


def _build(tmp_path: Path, script: list, content: str) -> AgentLoop:
    pending = PendingChanges(tmp_path)
    registry = ToolRegistry()
    registry.register(write_file_spec(tmp_path, pending))
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=OfflineTokenCounter()),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(
            project_root=tmp_path,
            context_window=4096,
            max_steps=8,
            supervise=False,
        ),
        # 与 cli/runtime.py 装配时同一个入口、同一个装配方式
        verify=make_verifier(tmp_path, pending),
    )


def _text_of(request) -> str:
    return "\n".join(message.content for message in request.messages)


@pytest.mark.skipif(
    shutil.which("node") is None, reason="node 不在 PATH：这一路自动验证不装配"
)
def test_写HTML后循环自己把结构结论顶回去(tmp_path: Path) -> None:
    script = [
        _turn(
            "写页面",
            [
                {
                    "name": "write_file",
                    "arguments": {"path": "game.html", "content": BAD_HTML},
                }
            ],
        ),
        _turn("完成", final="写好了"),
    ]
    loop = _build(tmp_path, script, BAD_HTML)
    assert loop.verify is not None, "没有 pytest 但 node 在，验证器必须装配出来"
    loop.run("写一个页面")

    prompt = _text_of(loop.gateway.requests[-1])
    assert "自动验证" in prompt
    assert '引用了不存在的 id "ghost"' in prompt
    # 位置要对回原文件（第 6 行），不是抽出来那段的行号
    assert "game.html:6" in prompt

@pytest.mark.skipif(
    shutil.which("node") is None, reason="node 不在 PATH：这一路自动验证不装配"
)
def test_HTML没问题时自动验证不再报失败(tmp_path: Path) -> None:
    script = [
        _turn(
            "写页面",
            [
                {
                    "name": "write_file",
                    "arguments": {"path": "game.html", "content": GOOD_HTML},
                }
            ],
        ),
        _turn("完成", final="写好了"),
    ]
    loop = _build(tmp_path, script, GOOD_HTML)
    loop.run("写一个页面")

    prompt = _text_of(loop.gateway.requests[-1])
    # 通过时循环那句话说「自动跑了一遍测试：通过」，失败时说「自动验证的结果」
    assert "自动" in prompt and "通过" in prompt
    assert "引用了不存在的 id" not in prompt
    # 通过也要说清「运行时行为没验」——不许让它以为通过就等于能用
    assert "查不了" in prompt
