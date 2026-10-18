"""HumanProgress：chat 散文进度行。"""

from spoolkit.cli.events import HumanProgress
from spoolkit.web.protocol import DELTA, FINAL_DELTA, NOTE, STEP, TOOL, WAIT


def test_step_tool_note_走stderr(capsys) -> None:
    progress = HumanProgress()
    progress.handle(STEP, {"n": 2})
    progress.handle(TOOL, {"name": "read_file", "ok": True})
    progress.handle(NOTE, {"text": "输出被截断，放大预算重试（max_tokens 100 → 200）", "ok": True})
    err = capsys.readouterr().err
    assert "第 2 步" in err
    assert "read_file → 成功" in err
    assert "放大预算重试" in err


def test_wait不重复打印(capsys) -> None:
    """wait 由 WaitDisplay 管；HumanProgress 忽略。"""
    HumanProgress().handle(WAIT, {"elapsed": 15, "limit": 245})
    assert capsys.readouterr().err == ""


def test_失败工具带detail(capsys) -> None:
    HumanProgress().handle(
        TOOL, {"name": "write_file", "ok": False, "detail": "路径越界"}
    )
    err = capsys.readouterr().err
    assert "write_file → 失败" in err
    assert "路径越界" in err


def test_delta追加正文(capsys) -> None:
    progress = HumanProgress()
    progress.handle(DELTA, {"text": "你好", "channel": "assistant"})
    progress.handle(DELTA, {"text": "世界", "channel": "assistant"})
    progress.finish()
    out = capsys.readouterr().out
    assert "你好" in out
    assert "世界" in out


def test_final_delta追加且不重放(capsys) -> None:
    progress = HumanProgress()
    progress.handle(FINAL_DELTA, {"text": "半截"})
    progress.handle(FINAL_DELTA, {"text": "续上"})
    progress.finish()
    out = capsys.readouterr().out
    assert out.count("半截") == 1
    assert "续上" in out


def test_截断note保留流式面板只打进度(capsys) -> None:
    """截断重试只打 note；面板保留，后续续写 delta 接着刷，不从头重来。"""
    progress = HumanProgress()
    progress.handle(DELTA, {"text": "半截", "channel": "assistant"})
    progress.handle(
        NOTE,
        {
            "text": "输出被截断，放大预算重试（max_tokens 100 → 200）",
            "ok": True,
            "kind": "truncation_retry",
        },
    )
    assert progress._stream is not None
    progress.handle(DELTA, {"text": "续写", "channel": "assistant"})
    progress.finish()
    captured = capsys.readouterr()
    assert "放大预算重试" in captured.err
    assert "半截" in captured.out
    assert "续写" in captured.out
