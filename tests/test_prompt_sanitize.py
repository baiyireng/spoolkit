"""回灌进提示的文本：不许带本次运行专属的临时/随机信息。

这一层钉的是那条通则（`spoolkit.prompt_sanitize` 模块头）：

    **任何回灌进提示的文本，都不得包含本次运行专属的临时/随机信息。**

可证伪的现场：两条 t=0 的 `add-feature`（同代码、相隔 66 秒）事件流 20 行里
18 行逐字节相同，唯一的分叉点是自动验证正文里那句 `PytestCacheWarning`——
它带着本次试跑副本的随机目录名 `…\\Temp\\…\\spool-trial-<8 位随机>\\project\\…`，
落在 `body[:400]` 的第 371 字，于是下一次请求的 token 数差了 2 个，贪心解码
把它放大成两句不同的收尾。

所以这里要证两件事，缺一条都不算数：

1. **进提示的那一份**：同一份内容、两个不同的随机副本名，清理之后**逐字相同**；
2. **给人看的那一份**（`ToolResult.raw`、事件流/日志的正文）：精确路径仍要
   一个字不差地留着——否则排查时照着它回不到现场。

第 1 条没有第 2 条对照，可以靠「把整段输出删光」作弊；第 2 条没有第 1 条对照，
可以靠「什么都不清」作弊。两条一起，只有一个做法能同时成立。
"""

from pathlib import Path

import pytest

from spoolkit import prompt_sanitize as ps
from spoolkit.agent.loop import AgentLoop
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.edit import PendingChanges
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.types import ToolResult
from spoolkit.tools.verify import condense_test_output, make_verifier

# 一次真实验证输出的形状：这句 `PytestCacheWarning` 就是那个分叉点。
# 用两条不同随机名的版本当「同一份内容的两次运行」。
PASSING = """退出码 0（成功）
$ python -m pytest -q
...                                                                      [100%]
=============================== warnings summary ===============================
{root}\\dsh-{session}\\spool-trial-{trial}\\project\\.pytest_cache
  {root}\\dsh-{session}\\spool-trial-{trial}\\project\\tests\\test_mod.py:1
    PytestCacheWarning: could not create cache path {root}\\dsh-{session}\\spool-trial-{trial}\\project\\.pytest_cache\\v\\cache\\nodeids: 拒绝访问
-- Docs: https://docs.pytest.org/en/stable/warnings.html
3 passed in 1.21s
"""

FAILING = """退出码 1（失败）
$ python -m pytest -q
F                                                                        [100%]
FAILED tests/test_mod.py::test_v - AssertionError: 1 != 2
"""


def _passing(session: str, trial: str) -> str:
    import tempfile

    return PASSING.format(
        root=Path(tempfile.gettempdir()), session=session, trial=trial
    )


def _failing(session: str, trial: str) -> str:
    import tempfile

    # 副本名刻意放在 **ERROR 行**里：`condense_test_output` 优先取的就是
    # `FAILED` / `ERROR` 那一行——不放进那里，那条路根本碰不到随机名，
    # 测试也就成了「怎么改都不红」的假钉。
    return FAILING + (
        "=========================== short test summary info ============================\n"
        "ERROR tests/test_mod.py::test_v - PermissionError: [WinError 5] 拒绝访问。: '"
        f"{Path(tempfile.gettempdir())}\\dsh-{session}\\spool-trial-{trial}"
        "\\project\\.pytest_cache'\n"
    )


# --------------------------------------------------------------------------
# 通则本体：四种形态各清一样东西
# --------------------------------------------------------------------------


def test_试跑副本与临时目录都换成占位符() -> None:
    text = (
        r"C:\Users\someone\AppData\Local\Temp" r"\dsh-RvaPU3\spool-trial-ab12cd34\project"
    )
    # 临时库那一段是**本机**的吗？不是就只能清试跑副本那一节——判据必须
    # 跟着环境走，不能写死用户名（写死的话这条测试在别人机器上假红）。
    cleaned = ps.for_prompt(text)
    assert "spool-trial-ab12cd34" not in cleaned
    assert ps.TRIAL_PLACEHOLDER in cleaned


def test_本机临时根整段换成占位符() -> None:
    import tempfile

    text = str(Path(tempfile.gettempdir()) / "dsh-abc123" / "spool-trial-9z8y7x")
    cleaned = ps.for_prompt(text)
    assert ps.TEMP_PLACEHOLDER in cleaned
    assert "abc123" not in cleaned and "9z8y7x" not in cleaned


def test_随机文件后缀只盖掉随机那一段() -> None:
    cleaned = ps.for_prompt("写不了 D:\\p\\.env.7fk2m9xq.tmp：拒绝访问")
    assert cleaned == "写不了 D:\\p\\.env.<随机名>.tmp：拒绝访问"


def test_普通标识符与路径不受影响() -> None:
    """清理**宁窄勿宽**：`python3.11`、`mod.py`、用户自己的工程路径都要留着。"""
    text = "D:\\proj\\mod.py python3.11 test_verify.py C:\\Users\\someone\\x.py"
    assert ps.for_prompt(text) == text


def test_幂等() -> None:
    """回灌点会嵌套（验证正文进循环、循环再拼进提示）：过两遍必须一样。"""
    once = ps.for_prompt(_passing("RvaPU3", "ab12cd34"))
    assert ps.for_prompt(once) == once


def test_同一份内容两个随机名清理后逐字相同() -> None:
    first = ps.for_prompt(_passing("RvaPU3", "ab12cd34"))
    second = ps.for_prompt(_passing("QQQQQQ", "zz99yy88"))
    assert first == second
    assert "ab12cd34" not in first and "zz99yy88" not in first


# --------------------------------------------------------------------------
# 落点一：`tools/verify.py` 的 condense 那条路
# --------------------------------------------------------------------------


def test_压缩后的验证正文两次逐字相同() -> None:
    first = condense_test_output(_failing("RvaPU3", "ab12cd34"))
    second = condense_test_output(_failing("QQQQQQ", "zz99yy88"))
    assert first == second, "进提示的那一份必须与随机副本名无关"
    assert "ab12cd34" not in first and "zz99yy88" not in first
    # 清理不许把判据本身弄丢：报错的主干还在
    assert "FAILED tests/test_mod.py::test_v" in first


def test_给人看的那份保留精确路径() -> None:
    """反向对照：清了提示侧，给人看的那一份一个字都不能少。"""
    raw = _failing("RvaPU3", "ab12cd34")
    assert condense_test_output(raw) != raw
    # 原始输出（落盘日志 / ToolResult.raw 的来源）没被碰过
    assert "spool-trial-ab12cd34" in raw


# --------------------------------------------------------------------------
# 落点二：`make_verifier` 的返回（成功那条路，就是实测里漏的那条）
# --------------------------------------------------------------------------


def _verifier_with(tmp_path: Path):
    """装一个真验证器，但把「起进程跑测试」这一步换成预设输出。"""
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "test_mod.py").write_text(
        "def test_v():\n    assert 1 == 1\n", encoding="utf-8"
    )
    pending = PendingChanges(tmp_path)
    verifier = make_verifier(tmp_path, pending)
    assert verifier is not None
    return verifier


def test_验证通过时正文也不许带随机副本名(tmp_path: Path, monkeypatch) -> None:
    """实测漏的就是这一条：**测试通过**时正文整段回灌，警告里的副本名跟着进去。"""
    seen: list[str] = []

    def fake_run_once(root, command, **kwargs):
        seen.append("run")
        output = _passing("RvaPU3" if len(seen) == 1 else "QQQQQQ", "ab12cd34")
        return ToolResult(ok=True, content=output, raw=output)

    monkeypatch.setattr("spoolkit.tools.verify.run_once", fake_run_once)
    verifier = _verifier_with(tmp_path)

    first = verifier([])
    second = verifier([])
    assert first.ok and second.ok
    assert first.content == second.content, "两次不同随机名，进提示的那份必须逐字相同"
    assert "ab12cd34" not in first.content


def test_验证的raw是给人看的证据不许清(tmp_path: Path, monkeypatch) -> None:
    """`raw` 是自动审查层的判据（也给人回现场用），必须留着精确路径。"""
    output = _passing("RvaPU3", "ab12cd34")

    def fake_run_once(root, command, **kwargs):
        return ToolResult(ok=True, content=output, raw=output)

    monkeypatch.setattr("spoolkit.tools.verify.run_once", fake_run_once)
    verifier = _verifier_with(tmp_path)

    result = verifier([])
    assert "spool-trial-ab12cd34" not in result.content
    assert "spool-trial-ab12cd34" in result.raw


# --------------------------------------------------------------------------
# 落点三：`AgentLoop._run_verification` 的结论（回灌进提示的那一段）
# --------------------------------------------------------------------------


def _loop(tmp_path: Path, verifier) -> AgentLoop:
    return AgentLoop(
        gateway=FakeModel(script=[], tokenizer=OfflineTokenCounter()),
        tokenizer=OfflineTokenCounter(),
        registry=ToolRegistry(),
        config=Config(project_root=tmp_path, context_window=8192, max_steps=2),
        verify=verifier,
        pending=PendingChanges(tmp_path),
    )


@pytest.mark.parametrize("ok", [True, False])
def test_验证结论进提示前已清理(tmp_path: Path, ok: bool) -> None:
    body = _passing("RvaPU3", "ab12cd34") if ok else _failing("RvaPU3", "ab12cd34")

    def verifier(changed):
        return ToolResult(ok=ok, content=body, raw=body)

    loop = _loop(tmp_path, verifier)
    report, passed = loop._run_verification([])
    assert passed is ok
    assert "ab12cd34" not in report
    assert "spool-trial-" not in report
    # 缓存下来的那一份（收尾报告要复用它）同样干净：否则它会以另一条路
    # 再次进提示，等于只堵了一半。
    assert loop._last_verify is not None
    assert "ab12cd34" not in loop._last_verify[0]


# --------------------------------------------------------------------------
# 落点四：**所有工具输出**共用的那个出口（新增工具也走这里）
# --------------------------------------------------------------------------


def _turn(thought: str, calls=None, final: str | None = None) -> str:
    import json

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


def test_任何工具输出进提示前都清理过(tmp_path: Path) -> None:
    """通则的兜底口：新加一个工具、它回一段带随机副本名的输出，也不许漏。

    这条是**端到端**的：假模型把每一次请求都记下来，逐条检查真正发出去的
    消息正文——只在某处单元函数上打补丁很容易漏掉另一条出口。
    """
    from spoolkit.tools.types import ToolSpec

    body = _passing("RvaPU3", "ab12cd34")
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="noisy_tool",
            description="回一段带本次副本路径的输出",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=lambda args: ToolResult(ok=True, content=body, raw=body),
        )
    )
    gateway = FakeModel(
        script=[
            _turn(
                "看一眼",
                [{"name": "noisy_tool", "arguments": {}, "why": "拿证据"}],
            ),
            _turn("做完了", final="做完了"),
        ],
        tokenizer=OfflineTokenCounter(),
    )
    loop = AgentLoop(
        gateway=gateway,
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(project_root=tmp_path, context_window=8192, max_steps=4),
        pending=PendingChanges(tmp_path),
    )
    loop.run("做点什么")

    assert gateway.requests, "假模型一次都没被调用，这条测试就什么都没证"
    prompts = [
        message.content for request in gateway.requests for message in request.messages
    ]
    # ① 工具输出**确实**回灌了（不然第二条断言是白过的）
    assert any("[noisy_tool] 成功" in text for text in prompts)
    # ② 回灌进来的那一份已经清理过：随机副本名一个字都不剩
    assert all("ab12cd34" not in text for text in prompts)
    assert all("spool-trial-" not in text for text in prompts)
    assert any(ps.TRIAL_PLACEHOLDER in text for text in prompts)
