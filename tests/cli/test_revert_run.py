"""闸门 3：整轮回退 + 收尾就把撤销命令摆到眼前。

真实经历（这一组用例钉的就是它）：用户**接受了一份「最终验证未通过」的改动**，
事后反悔，用系统外的手段（自己找备份 / 编辑器）退了回去——而 `spool revert`
明明存在。三个缺口：

1. **粒度只到「上一次」**：一轮里常改 N 处（多步 replace_lines），要退整轮
   得反复 revert、还得自己理顺序；
2. **可发现性**：收尾报告报了「共落成 N 处改动」，却没有跟着一句可复制的
   撤销命令；
3. **结论不醒目**：大 diff 抢眼，「最终验证：未通过」被埋在下面。

每一条都有一条用例，且都用**真跑的字节**或**真的执行那条命令**来证伪。
"""

import json
import re
from pathlib import Path

from spoolkit import runlog
from spoolkit.agent.loop import AgentLoop
from spoolkit.cli.app import main
from spoolkit.cli.settle import verification_header
from spoolkit.config import Config
from spoolkit.llm.fake import FakeModel
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.tools.edit import (
    PendingChanges,
    delete_file_spec,
    register_edit_tools,
    replace_lines_spec,
    write_file_spec,
)
from spoolkit.tools.fs import read_file_spec
from spoolkit.tools.registry import ToolRegistry
from spoolkit.tools.types import ToolResult


# ---- 现场：一轮（多轮）里落成 N 处改动，含 1 个删除 ---------------------------


def _workspace(tmp_path: Path) -> dict[str, bytes]:
    """原始工作区，返回「改动前」的逐字节快照。"""
    (tmp_path / "keep.py").write_text("v1 = 1\nv1b = 2\nv1c = 3\n", encoding="utf-8")
    (tmp_path / "gone.py").write_text(
        "<html>贵</html>\n共 2 行\n", encoding="utf-8"
    )
    return {
        name: (tmp_path / name).read_bytes() for name in ("keep.py", "gone.py")
    }


def _land_two_rounds(tmp_path: Path) -> str:
    """同一轮（同一个 run id）里落两次盘：先改 keep.py + 新建 added.py，
    再改 keep.py + 删 gone.py。

    为什么同一个文件要跨两次落盘：「按改动逆序退」这条只有在这种形状下才
    看得出对错——顺序反了，先退的那一处会因为「内容对不上」被跳过，
    最后留在磁盘上的是中途那一版，而不是改动前的原文。

    这正是**按步执行**那条路的真实形状：一次入口 = 一轮，每一步落一次盘，
    同一步里改的文件下一步很可能再改一次。
    """
    run_id = runlog.new_run_id()
    first = PendingChanges(tmp_path, run_id=run_id)
    write_file_spec(tmp_path, first).handler(
        {"path": "keep.py", "content": "v2 = 1\nv2b = 2\nv1c = 3\n"}
    )
    write_file_spec(tmp_path, first).handler(
        {"path": "added.py", "content": "print('added')\n"}
    )
    first.apply()

    second = PendingChanges(tmp_path, run_id=run_id)
    replace_lines_spec(tmp_path, second).handler(
        {
            "path": "keep.py",
            "start_line": 3,
            "end_line": 3,
            "content": "v3c = 3\n",
            "expect": "v1c = 3",
        }
    )
    delete_file_spec(tmp_path, second).handler(
        {"path": "gone.py", "reason": "内容已并入 keep.py"}
    )
    second.apply()
    return run_id


# ---- 验收 1：整轮回退，逐处字节等于改动前（含被删文件恢复） -------------------


def test_整轮回退之后逐处字节等于改动前_含被删文件(tmp_path: Path, capsys) -> None:
    before = _workspace(tmp_path)
    run_id = _land_two_rounds(tmp_path)

    # 落盘之后确实是「改了 N 处」：两次改 keep.py、一次新建、一次删除。
    assert (tmp_path / "keep.py").read_bytes() != before["keep.py"]
    assert (tmp_path / "added.py").is_file()
    assert not (tmp_path / "gone.py").exists()

    assert main(["revert", "--root", str(tmp_path), "--run", run_id]) == 0
    out = capsys.readouterr().out

    # 逐处字节等于改动前（不是「行数对了」也不是「内容像」）。
    assert (tmp_path / "keep.py").read_bytes() == before["keep.py"]
    assert (tmp_path / "gone.py").read_bytes() == before["gone.py"]
    # 恢复被删的文件 —— 是**还原内容**，不是留一个空文件。
    assert (tmp_path / "gone.py").read_text(encoding="utf-8") != ""
    # 这一轮自己造出来的文件回到「不存在」。
    assert not (tmp_path / "added.py").exists()

    # 报告逐处可见，而且是**逆序**：先退最后落的那一处。
    for name in ("keep.py", "gone.py", "added.py"):
        assert name in out
    assert "已恢复" in out and "共 4 处" in out
    assert out.index("gone.py") < out.index("added.py") < out.rindex("keep.py")


def test_落成改动会写进运行记录(tmp_path: Path) -> None:
    """运行要可识别：改了什么、改动前是什么、起止时间、验证结论，都落成一份。"""
    _workspace(tmp_path)
    run_id = _land_two_rounds(tmp_path)
    record = json.loads(
        (tmp_path / ".agent" / "runs" / run_id / "run.json").read_text(encoding="utf-8")
    )

    assert record["id"] == run_id
    assert record["started_at"] and record["finished_at"]
    paths = [
        entry["path"] for round_ in record["rounds"] for entry in round_["entries"]
    ]
    assert paths == ["keep.py", "added.py", "keep.py", "gone.py"]
    # 每一处都带着改动前的原文（baseline）——没有它就没有退路。
    gone = record["rounds"][1]["entries"][1]
    assert gone["deleted"] is True
    assert gone["old_text"] == "<html>贵</html>\n共 2 行\n"


def test_预演只说会做什么_磁盘一个字不动(tmp_path: Path, capsys) -> None:
    before = _workspace(tmp_path)
    run_id = _land_two_rounds(tmp_path)
    landed_keep = (tmp_path / "keep.py").read_bytes()

    assert main(["revert", "--root", str(tmp_path), "--dry-run", "--run", run_id]) == 0
    out = capsys.readouterr().out

    assert "预演" in out and "会恢复" in out
    # 磁盘一个字没动：改过的还是改过的，删掉的还是删掉的。
    assert (tmp_path / "keep.py").read_bytes() == landed_keep
    assert not (tmp_path / "gone.py").exists()
    assert (tmp_path / "keep.py").read_bytes() != before["keep.py"]


def test_最近一轮可以按名字找回来(tmp_path: Path, capsys) -> None:
    before = _workspace(tmp_path)
    _land_two_rounds(tmp_path)

    assert main(["revert", "--root", str(tmp_path), "--last-run"]) == 0
    capsys.readouterr()
    assert (tmp_path / "keep.py").read_bytes() == before["keep.py"]
    assert (tmp_path / "gone.py").read_bytes() == before["gone.py"]


def test_退过的不会再退第二次(tmp_path: Path, capsys) -> None:
    """重复跑同一条命令不许冲掉之后的手工改动。"""
    _workspace(tmp_path)
    run_id = _land_two_rounds(tmp_path)
    assert main(["revert", "--root", str(tmp_path), "--run", run_id]) == 0
    capsys.readouterr()

    # 退完之后人又改了一笔：第二次调用不该再动它。
    (tmp_path / "keep.py").write_text("手工写的\n", encoding="utf-8")
    assert main(["revert", "--root", str(tmp_path), "--run", run_id]) == 0
    out = capsys.readouterr().out
    assert "跳过" in out
    assert (tmp_path / "keep.py").read_text(encoding="utf-8") == "手工写的\n"


# ---- 验收 2：冲突拒绝覆盖（变异：去掉冲突检查必红） ---------------------------


def test_落盘之后又被改过的文件拒绝覆盖并列出冲突路径(
    tmp_path: Path, capsys
) -> None:
    before = _workspace(tmp_path)
    run_id = _land_two_rounds(tmp_path)
    # 「另有人改了同一处」：落盘之后 keep.py 又被改过。
    (tmp_path / "keep.py").write_text("别人后来的改动\n", encoding="utf-8")

    code = main(["revert", "--root", str(tmp_path), "--run", run_id])
    out = capsys.readouterr().out

    # 拒绝覆盖：那一处必须原样留着（不许被改动前的原文冲掉）。
    assert (tmp_path / "keep.py").read_text(encoding="utf-8") == "别人后来的改动\n"
    assert before["keep.py"] != (tmp_path / "keep.py").read_bytes()
    # 逐条列出冲突路径 + 人工怎么处理。
    assert "冲突" in out
    assert "拒绝覆盖" in out
    assert "keep.py" in out
    assert "人工处理" in out
    # 没冲突的那几处照退：删除的文件恢复、新建的文件删掉。
    assert (tmp_path / "gone.py").read_bytes() == before["gone.py"]
    assert not (tmp_path / "added.py").exists()
    # 有冲突时退出码非零：脚本化使用的人要能看出来「没有全部退回去」。
    assert code == 3


def test_没有这条运行记录时说人话(tmp_path: Path, capsys) -> None:
    _workspace(tmp_path)
    assert main(["revert", "--root", str(tmp_path), "--run", "20260101-000000-0000"]) == 2
    err = capsys.readouterr().err
    assert "没有这条运行记录" in err


# ---- 验收 3：收尾报告里的撤销命令，而且它真能跑通 ------------------------------


def _edit_loop(tmp_path: Path, script: list, *, verify) -> tuple[AgentLoop, PendingChanges]:
    pending = PendingChanges(tmp_path)
    registry = ToolRegistry()
    registry.register(read_file_spec(tmp_path, pending))
    register_edit_tools(registry, tmp_path, pending)
    loop = AgentLoop(
        gateway=FakeModel(script=script, tokenizer=OfflineTokenCounter()),
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=Config(project_root=tmp_path, context_window=8192, max_steps=4),
        verify=verify,
        pending=pending,
    )
    return loop, pending


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


def test_收尾报告里的撤销命令照着跑就能整轮退回去(tmp_path: Path) -> None:
    """报告里那一行不是装饰：用例**把它抄下来执行**，然后验字节。"""
    before = _workspace(tmp_path)
    loop, pending = _edit_loop(
        tmp_path,
        [
            _turn(
                "改一处",
                [
                    {
                        "name": "write_file",
                        "arguments": {"path": "keep.py", "content": "v2 = 1\n"},
                    }
                ],
            ),
            _turn("再删一份", [{"name": "delete_file",
                              "arguments": {"path": "gone.py", "reason": "并入 keep.py"}}]),
            _turn("好了", final="改完了"),
        ],
        verify=lambda changed: ToolResult(ok=True, content="全部通过"),
    )

    result = loop.run("改点东西")
    assert "本次运行共落成" in result.final

    found = re.search(r"spool revert --run (\S+)", result.final)
    assert found, f"收尾报告里没有可复制的撤销命令：{result.final!r}"
    run_id = found.group(1)
    assert run_id == pending.run_id

    # 落盘（收尾处理做的事）之后，照抄那一行执行。
    pending.apply()
    assert (tmp_path / "keep.py").read_bytes() != before["keep.py"]
    assert not (tmp_path / "gone.py").exists()

    assert main(["revert", "--root", str(tmp_path), "--run", run_id]) == 0
    assert (tmp_path / "keep.py").read_bytes() == before["keep.py"]
    assert (tmp_path / "gone.py").read_bytes() == before["gone.py"]


# ---- 验收 4：验证未通过时结论与命令在 diff 之前（用例断言顺序） ---------------

_DIFF_BLOCK = "--- a/keep.py\n+++ b/keep.py\n-v1 = 1\n+v2 = 1\n"


def test_验证未通过时结论与撤销命令排在diff之前(tmp_path: Path) -> None:
    """大 diff 抢眼，「最终验证：未通过」原先被埋在后面。

    这里让模型自己那段结论**带着 diff**（模型真的常这么写），然后断言：
    判词与撤销命令都在那段 diff **之前**。
    """
    _workspace(tmp_path)
    loop, pending = _edit_loop(
        tmp_path,
        [
            _turn(
                "改一处",
                [
                    {
                        "name": "write_file",
                        "arguments": {"path": "keep.py", "content": "v2 = 1\n"},
                    }
                ],
            ),
            _turn("改完了", final=f"我改了这一处：\n{_DIFF_BLOCK}"),
        ],
        verify=lambda changed: ToolResult(
            ok=False, content="测试没有通过：assert v1 == 2 失败", raw="assert 失败"
        ),
    )

    report = loop.run("改点东西").final

    assert "最终验证：未通过" in report
    assert "spool revert --run" in report
    assert report.index("最终验证：未通过") < report.index(_DIFF_BLOCK)
    assert report.index("spool revert --run") < report.index(_DIFF_BLOCK)
    # 失败要点也要在最前（不是埋在验证正文里）。
    assert report.index("失败要点") < report.index(_DIFF_BLOCK)


def test_收尾处理把结论打在diff之前(tmp_path: Path, capsys) -> None:
    """终端那条路：`settle` 是打 diff 的那一处，结论必须由它先打。

    变异：把 `settle` 的 header 那两行去掉 → 这里红（判词跑到 diff 后面）。
    """
    from spoolkit.cli.settle import settle
    from spoolkit.policy import ASK

    (tmp_path / "keep.py").write_text("v1 = 1\n", encoding="utf-8")
    pending = PendingChanges(tmp_path)
    write_file_spec(tmp_path, pending).handler(
        {"path": "keep.py", "content": "v2 = 1\n"}
    )

    header = verification_header(False, "测试没有通过", "spool revert --run abc")
    settle(pending, ASK, (), ask=lambda _: "n", header=header)
    out = capsys.readouterr().out

    assert "最终验证：未通过" in out and "spool revert --run abc" in out
    assert "--- a/keep.py" in out
    assert out.index("最终验证：未通过") < out.index("--- a/keep.py")
    assert out.index("spool revert --run abc") < out.index("--- a/keep.py")


# ---- 不变量：给不出 baseline 的改动不许落盘 ----------------------------------


def test_给不出基线的改动不落盘并在报告里说明原因(tmp_path: Path, capsys) -> None:
    """二进制文件（改动前的内容读不出来）→ 没有退路，所以不许落盘。

    这是那条不变量允许的**唯一**一种「扣成果」，所以必须有声音：
    扣掉要写进报告，否则用户看到一个「安静地少做了一处」的运行。
    """
    from spoolkit.cli.settle import settle
    from spoolkit.policy import AUTO

    blob = bytes([0xFF, 0xFE, 0x00, 0x80]) + b"binary"
    (tmp_path / "data.bin").write_bytes(blob)
    pending = PendingChanges(tmp_path)
    write_file_spec(tmp_path, pending).handler(
        {"path": "data.bin", "content": "text now\n"}
    )

    action, written = settle(
        pending, AUTO, ("**",), baseline_path=tmp_path / ".agent" / "last_change.json",
        ask=lambda _: "y",
    )
    out = capsys.readouterr().out

    # 这一轮什么也没落成（成果被扣掉了），而**文件一个字没动**。
    assert (written, action) == ([], "auto")
    assert (tmp_path / "data.bin").read_bytes() == blob, "扣成果之后文件被改动了"
    assert "没有落盘" in out
    assert "data.bin" in out
    assert "读不出来" in out
