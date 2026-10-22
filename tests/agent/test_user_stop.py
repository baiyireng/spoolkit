"""用户停止保留已提交的节。终稿阶段的插话不打断当前节。"""

from spoolkit.agent.writing_state import (
    INTERRUPT_FINAL,
    OutlineSection,
    WritingState,
    apply_user_stop,
)


def _state() -> WritingState:
    outline = tuple(
        OutlineSection(title, "要点", 400) for title in ("背景", "问题", "方案")
    )
    return WritingState(
        goal="写清楚",
        outline=outline,
        completed=(0, 1),
        next_section=2,
    )


def test_停在节中保留已提交并把当前节标未完成() -> None:
    effect = apply_user_stop(_state(), phase="section", note="先停")
    assert effect.completed == (0, 1)
    assert effect.incomplete == 2
    assert effect.queued_note == ""
    assert effect.cancel_current is True


def test_终稿插话排队且不把节标成未完成() -> None:
    assert INTERRUPT_FINAL is False
    effect = apply_user_stop(_state(), phase="final", note="改一下口径")
    assert effect.completed == (0, 1)
    assert effect.incomplete is None
    assert effect.queued_note == "改一下口径"
    assert effect.cancel_current is True
