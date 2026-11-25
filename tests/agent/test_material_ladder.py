"""S6：预算阶梯——**先压素材，压完仍超硬线才重置**。

五件事，每一件都能证伪：

1. 过整理线就压一遍，**不重置**：压缩稿（带「已压缩」抬头）进了下一轮提示，
   而 `resets` 还是 0；
2. 判据原文（那行 `FAILED`）在压缩之后**逐字还在**——保护通道不是模型自觉；
3. 压完仍超硬线才重置，且挂回去的是**压缩后那份**（有判据原文抬头、没有
   机械卡片抬头）；
4. 叫压缩模型时是**干净上下文**：只有问题、调用声明与素材正文，没有 system、
   没有工作方式、没有历史；
5. 模型编造的行一行都注入不进去；`compaction=False` 回到旧路（机械卡片）；
   真装配路径上默认开着、脚本化路径关着。
6. 压完之后的接缝也对：工具结果原文被换成压缩稿了，所以**同一次读取要能重新
   执行**（走「刷新读取」通道），而不是被重复调用护栏再拦一次。

另有一条容易被忽略的：**每个 step 最多压一次**。阶梯第一级每一步都会重新判一次
预算，于是「压一遍、这步就不再压」必须钉死——否则同一份素材可能在一轮运行里被
反复送进模型。真机上素材**每一步都在变**（实测：被放行的重复读取正文带了「刷新」
抬头、越界写的拒绝话术也会记进素材），所以这里不假设"素材不变"，只钉"每步一次、
读数对得上"。压缩调用的**总量**由 B 类真机读数说话（S6 验收那条）。

为什么单开一个文件：`tests/agent/test_converge_budget.py` 量的是旧的重置路（它给
`AgentLoop` 传 `compaction=False`），这里量的才是新加的这一级。
"""

import json
from pathlib import Path

from spoolkit.agent.loop import GUARD_REFRESH_KIND, AgentLoop
from spoolkit.cli.runtime import assemble_loop, build_loop
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.edit import PendingChanges, write_file_spec
from spoolkit.tools.fs import list_dir_spec, read_file_spec
from spoolkit.tools.registry import ToolRegistry

_GOAL = "定位测试为什么挂"
_LOG = "logs/run.log"
_SECTION = "【素材（已压缩；判据原文逐字保留）】"
_PROTECTED = "【判据原文（逐字，未改写）】"
_CARD = "【重置前拿到的素材】"
# 30 条判据行：机械层保它们逐字直通、不参与条数上限，于是机械层一个人就超过了
# 整理线算出来的「压完最多留多少」（600），模型层才真的会被叫起来。
_FAILED_SAMPLE = (
    "FAILED tests/services/test_services.py::test_case_00 "
    "- AssertionError: assert state == 'stopped'"
)
_STUB = "- [read_file logs/run.log] 三十条同一条断言失败"
# 门槛全是标定值，测试里要把它们拧到「这几百 token 就能触发」的位置，否则一份
# 日志根本到不了真机那种尺寸。
_TIGHT = {
    "material_compact_ratio": 0.2,
    "material_compact_share": 0.2,
}


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


def _read(path: str) -> dict:
    return {"name": "read_file", "arguments": {"path": path}}


def _failed(index: int) -> str:
    return (
        f"FAILED tests/services/test_services.py::test_case_{index:02d} "
        "- AssertionError: assert state == 'stopped'"
    )


def _noise(index: int) -> str:
    return (
        f"2026-10-06 12:00:{index:02d} DEBUG poller tick={index} queue=3 depth=7 "
        "worker=spool-worker-1 elapsed=0.03ms retry=0 "
        "note=heartbeat received from supervisor and the worker pool is healthy"
    )


class _Recorder:
    """记录型压缩器：**只记不猜**——它收到什么、被叫了几次，都留在这里。"""

    def __init__(self, answer: str = _STUB) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answer


def _logs(tmp_path: Path) -> None:
    (tmp_path / "logs").mkdir(exist_ok=True)
    body = "\n".join(
        [_failed(index) for index in range(30)]
        + [_noise(index) for index in range(8)]
    )
    (tmp_path / _LOG).write_text(body + "\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("x = 0\n", encoding="utf-8")


def _build(
    tmp_path: Path,
    script: list,
    *,
    overrides: dict | None = None,
    compactor: _Recorder | None = None,
    compaction: bool = True,
    max_steps: int = 6,
    events: list | None = None,
) -> AgentLoop:
    _logs(tmp_path)
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path))
    registry.register(list_dir_spec(tmp_path))
    registry.register(write_file_spec(tmp_path, PendingChanges(tmp_path)))
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=OfflineTokenCounter()),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(
            project_root=tmp_path,
            context_window=4096,
            max_steps=max_steps,
            supervise=False,
            overrides={**_TIGHT, **(overrides or {})},
        ),
        compactor=compactor,
        compaction=compaction,
        on_event=(
            None
            if events is None
            else lambda kind, data: events.append((kind, dict(data)))
        ),
    )


def _prompts(loop: AgentLoop) -> list[str]:
    """每一轮发给**主循环模型**的全文（压缩器那一份另算，见 `_Recorder`）。"""
    return [
        "\n".join(message.content for message in request.messages)
        for request in loop.gateway.requests
    ]


def test_过整理线就压一遍且不重置(tmp_path: Path) -> None:
    """阶梯第一级：压完装得下，就不动历史以外的任何东西。"""
    stub = _Recorder()
    loop = _build(
        tmp_path,
        [
            _turn("先看日志", [_read(_LOG)]),
            _turn("够了", final="结论：同一条断言失败了三十次。"),
        ],
        compactor=stub,
    )
    result = loop.run(_GOAL)
    prompts = _prompts(loop)
    assert len(stub.prompts) == 1
    assert result.compactions == 1
    assert result.resets == 0, "压完装得下，不该重置"
    assert _SECTION in prompts[-1], "压缩稿没进下一轮提示"
    assert _FAILED_SAMPLE in prompts[-1], "判据原文没跟着压缩稿一起留下"
    assert sum("素材压缩" in line for line in result.trace) == 1
    assert result.final.strip(), "压缩不该把最终答复吞掉"


def test_每个step最多压一次且读数对得上(tmp_path: Path) -> None:
    """三步走：中间那步的读数会让素材变一点，压缩次数必须与真调用次数一致。"""
    stub = _Recorder()
    loop = _build(
        tmp_path,
        [
            _turn("先看日志", [_read(_LOG)]),
            # 同一次读数再要一遍：上一遍压完抬了上下文版本，于是它被当成「刷新」
            # 放行、正文带上刷新抬头——素材于是真的变了（实测如此）。
            _turn("再看一眼", [_read(_LOG)]),
            _turn("够了", final="结论：同一条断言失败了三十次。"),
        ],
        compactor=stub,
    )
    result = loop.run(_GOAL)
    assert len(stub.prompts) == result.compactions, "读数与实际调用次数不一致"
    assert 1 <= result.compactions <= result.steps, "一个 step 压了不止一次"
    assert result.resets == 0
    assert _FAILED_SAMPLE in _prompts(loop)[-1]


def test_压缩之后同一次读取还能重新取(tmp_path: Path) -> None:
    """压缩把工具结果原文换成了压缩稿，那之后同一次读取必须**重新执行**。

    这一条量的是压缩与重复调用护栏之间的接缝：原文已经不在模型手上，再拦一次
    「重复调用」等于让模型只能对着摘要发呆、拿不到新读数。`context_revision`
    就是这条缝的开关——忘了推它，这里会看到一次硬拦而不是一次刷新。
    """
    events: list = []
    stub = _Recorder()
    loop = _build(
        tmp_path,
        [
            _turn("先看日志", [_read(_LOG)]),
            _turn("再看一眼", [_read(_LOG)]),
            _turn("够了", final="结论：同一条断言失败了三十次。"),
        ],
        compactor=stub,
        events=events,
    )
    result = loop.run(_GOAL)
    assert result.compactions >= 1, "这一轮压根没压，接缝没被碰到"
    reasons = [
        str(data.get("reason", ""))
        for kind, data in events
        if kind == "note" and data.get("kind") == GUARD_REFRESH_KIND
    ]
    assert reasons, "压缩之后同一次读取被拦成了重复调用：模型拿不到新读数"
    assert any("压缩" in reason for reason in reasons), (
        f"放行的理由里没有一条是「原结果被压缩丢弃」：{reasons}"
    )


def test_压缩不了的那部分判据原文逐字还在(tmp_path: Path) -> None:
    """保护通道的结构性证据：30 条判据行一条不少地出现在压缩稿里。"""
    stub = _Recorder()
    loop = _build(
        tmp_path,
        [_turn("先看日志", [_read(_LOG)]), _turn("够了", final="写完了。")],
        compactor=stub,
    )
    loop.run(_GOAL)
    last = _prompts(loop)[-1]
    assert _PROTECTED in last
    for index in range(30):
        assert _failed(index) in last, f"第 {index} 条判据原文被压没了"


def test_压完仍超硬线才重置且卡片是压缩后那份(tmp_path: Path) -> None:
    """阶梯第二级：硬线拧到很低，逼迫它在压缩之后仍然重置。"""
    stub = _Recorder()
    loop = _build(
        tmp_path,
        [_turn("先看日志", [_read(_LOG)]), _turn("够了", final="写完了。")],
        overrides={"hard_trigger_ratio": 0.01},
        compactor=stub,
    )
    result = loop.run(_GOAL)
    last = _prompts(loop)[-1]
    assert len(stub.prompts) == 1
    assert result.compactions == 1
    assert result.resets >= 1
    assert _PROTECTED in last, "重置挂的不是压缩后那份"
    assert _FAILED_SAMPLE in last
    assert _CARD not in last, "压缩稿被机械卡片顶掉了，等于白压"


def test_叫压缩模型时是干净上下文(tmp_path: Path) -> None:
    """压缩提示里只有问题、调用声明与素材正文——没有 system、没有历史。"""
    stub = _Recorder()
    loop = _build(
        tmp_path,
        [_turn("先看日志", [_read(_LOG)]), _turn("够了", final="写完了。")],
        compactor=stub,
    )
    loop.run(_GOAL)
    assert len(stub.prompts) == 1
    prompt = stub.prompts[0]
    assert "【问题】" in prompt and _GOAL in prompt
    assert "- [read_file logs/run.log] （没有说明）" in prompt
    assert "【待压缩正文】" in prompt
    assert "工作方式" not in prompt, "system 那段被背进来了"
    assert _SECTION not in prompt, "上一遍的压缩稿又被喂回去了"
    assert _FAILED_SAMPLE not in prompt, "判据原文不该进模型输入"


def test_模型编造的行注入不进去(tmp_path: Path) -> None:
    """一行引用素材里没有的路径 → 整行拦下；真判据原文不受影响。"""
    stub = _Recorder(
        answer="\n".join(
            [
                "- [read_file logs/run.log] 三十条同一条断言失败",
                "- [read_file logs/run.log] 还挂了 tests/ghost/test_ghost.py 里的用例",
            ]
        )
    )
    loop = _build(
        tmp_path,
        [_turn("先看日志", [_read(_LOG)]), _turn("够了", final="写完了。")],
        compactor=stub,
    )
    result = loop.run(_GOAL)
    last = _prompts(loop)[-1]
    assert "tests/ghost" not in last, "编造的文件名被写进了上下文"
    assert _FAILED_SAMPLE in last
    assert result.final.strip()


def test_关掉这一级就回到旧的重置路(tmp_path: Path) -> None:
    """`compaction=False`：不叫压缩模型，重置时挂机械卡片。"""
    stub = _Recorder()
    loop = _build(
        tmp_path,
        [_turn("先看日志", [_read(_LOG)]), _turn("够了", final="写完了。")],
        overrides={"hard_trigger_ratio": 0.01},
        compactor=stub,
        compaction=False,
    )
    result = loop.run(_GOAL)
    prompts = _prompts(loop)
    assert stub.prompts == [], "关掉了还在叫压缩模型"
    assert result.compactions == 0
    assert result.resets >= 1
    assert _CARD in prompts[-1]
    assert all(_SECTION not in prompt for prompt in prompts)


def test_真装配默认开而脚本化路径关(tmp_path: Path) -> None:
    """接线点本身：生产默认开，假模型驱动的离线路径关。"""
    gateway = FakeModel(script=[], tokenizer=OfflineTokenCounter())
    loop = assemble_loop(
        tmp_path,
        gateway,
        Config(project_root=tmp_path, context_window=4096, supervise=False),
    )
    assert loop._compaction is True
    assert loop._compactor is not None
    assert build_loop(tmp_path, [])._compaction is False
