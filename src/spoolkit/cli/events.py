"""内核侧的事件输出器。

`--events` 模式下，stdout 上只应该有 JSON 行——不夹杂任何散文。
散文会被解析器忽略，但录下来的文件也会变脏，调试时难读。

每条都 flush：不 flush 的话输出攒在缓冲区里，UI 那边看起来像卡住了，
而你会先去怀疑网络。

散文模式（`spool chat`）用 `HumanProgress`：同源事件语义，出口是 stderr，
不占 stdout 结论通道。`wait` 仍由 `bind_model_wait` / `WaitDisplay` 管。
"""

import sys
from pathlib import Path
from typing import Callable, Sequence
from typing import Any, TextIO

from spoolkit.agents.plan import out_of_scope
from spoolkit.cli.approval import apply_with_audit
from spoolkit.cli.settle import refused_note, unconfirmed_note
from spoolkit.cli.term import print_err
from spoolkit.policy import AUTO
from spoolkit.tools.edit import PendingChanges, save_baseline
from spoolkit.web.protocol import (
    AWAIT,
    CONFIRM,
    DELTA,
    DIFF,
    FINAL_DELTA,
    Event,
    NOTE,
    SECTION_DONE,
    SECTION_START,
    STEP,
    TOOL,
    WAIT,
)


class EventWriter:
    """把事件按行写到流上。"""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout

    def _write(self, kind: str, data: dict) -> None:
        self._stream.write(Event(kind, data).to_line() + "\n")
        self._stream.flush()

    def emit(self, kind: str, /, **data: Any) -> None:
        """发一个事件。`kind` 是**位置限定**的（那个 `/` 是这条不变式本身）。

        为什么必须限定：`note` 的载荷里也有一个 `kind` 字段（`audit` /
        `stale_references` / `tests_weakened` / `escalation` / `pending_actions`…），
        而这里的形参就叫 `kind`。不限定位置时，`writer.emit(NOTE, kind="escalation")`
        会抛 `TypeError: got multiple values for argument 'kind'`——正是 `handle`
        那次实测事故的同一个坑（见 `handle` 的说明）。限定位置之后，`kind=`
        一律落进 `**data`：那个写法从"会炸"直接变成"就是对的"。

        **约束交给签名，不交给记性**：靠注释提醒的结果是——同一个坑修了
        `handle` 一处，`run.py` 里还留着两处（`mypy` 的
        `[misc] gets multiple values for keyword argument` 当场抓出来）。
        """
        self._write(kind, dict(data))

    def emit_note(
        self, text: str, *, ok: bool = True, kind: str = "", **extra: Any
    ) -> None:
        """发一条 `note`。

        `kind` 在这里是**载荷字段**（`audit` / `tests_weakened` / `escalation`…），
        探针按它计数；它不是位置参数，所以不怕与 `emit` 的形参重名。
        要带载荷字段的 note 走这一个，语义一目了然。
        """
        payload: dict[str, Any] = {"text": text, "ok": ok}
        if kind:
            payload["kind"] = kind
        payload.update(extra)
        self._write(NOTE, payload)

    def handle(self, kind: str, data: dict) -> None:
        """签名与 AgentLoop 的 on_event 一致，可直接传进去。

        **不许写成 `self.emit(kind, **data)`**：`note` 事件里带一个 `kind`
        字段（`audit` / `stale_references` / `tests_weakened` …，探针按它计数），
        而它会与 `emit(self, kind, ...)` 的位置参数重名，抛
        `TypeError: emit() got multiple values for argument 'kind'`——那个异常
        被 `AgentLoop._emit` 的 `except Exception` 吞掉，于是**所有带 kind 的
        note 都静默丢失**。实测现场：013137-rename-cross-file-r0 的事件流里
        8 条 note 全是**不带 kind** 的那种，`guard_refresh` /
        `supervisor_fact_conflict` / `audit` 一条都没有——探针里
        `supervisor_false_claims` 常年为 0 就是这个原因，不是督导从不说错。
        """
        self._write(kind, dict(data))


def _to_stderr(line: str) -> None:
    """人读的散文（diff、留档、审计行）在 events 模式下的出口。

    选 stderr 而不是另开日志文件：探针与桥已经在分别收 stdout / stderr，
    多一个文件就多一个「谁负责清理」的问题；而 stderr 不参与 JSON 行协议，
    谁想看就看，谁不想看就当它不存在。
    """
    print(line, file=sys.stderr)
    sys.stderr.flush()


def settle_with_events(
    pending: PendingChanges,
    policy: str,
    scope: Sequence[str],
    baseline_path: Path | None,
    writer: EventWriter,
    reader: Callable[[], str] | object = None,
    header: str = "",
) -> str:
    """按策略处理待落盘改动，全程以事件表达。

    语义与终端模式完全一致，只是把「打印 diff 并问一句」换成了
    「发 diff 与 await，再从 stdin 读一行」。**两条路径的判定必须一样**，
    否则同一个策略在终端和网页里表现不同——那种差异不会报错，
    只会让人困惑「为什么这里自动、那里不自动」。

    `header`（验证未通过那一段）**先于 DIFF 发出去**：网页壳按事件顺序渲染，
    大 diff 排在前面时，「最终验证：未通过 + 撤销命令」就沉到下面去了——
    用户正是这么接受了一份没通过验证的改动。
    """
    changes = pending.items()
    if header:
        writer.emit(NOTE, text=header, ok=False)
    if not changes:
        # 没有待确认改动，但可能还有「清单没交代完、因此没落盘」的暂存重写。
        # 那句必须发出去：不吭声就是静默丢弃模型真交出来的正文。
        note = unconfirmed_note(pending)
        if note:
            writer.emit(NOTE, text=note, ok=False)
        refused = refused_note(pending)
        if refused:
            writer.emit(NOTE, text=refused, ok=False)
        return "none"

    for change in changes:
        if change.declared:
            writer.emit(NOTE, text=f"{change.path}：{change.declared}", ok=True)
        writer.emit(DIFF, path=change.path, text=change.diff)

    if policy == AUTO and not out_of_scope([c.path for c in changes], scope):
        if not any(pending.is_linked(c.path) for c in changes):
            # diff 与人读的留档走 **stderr**：stdout 上只能有 JSON 行。
            # 实测把 diff 打到 stdout 时，一条记录里混进 36 行散文，
            # `--events` 的解析器要么崩、要么被迫打补丁，录下来的文件也读不了。
            written = apply_with_audit(pending, baseline_path, out=_to_stderr)
            _emit_refused(writer, pending)
            writer.emit(CONFIRM, applied=True, auto=True, count=len(written))
            return "auto"

    writer.emit(AWAIT, count=len(changes))
    source = reader if reader is not None else sys.stdin
    answer = (source.readline() or "").strip().lower()
    apply = answer in ("y", "yes")
    writer.emit(CONFIRM, applied=apply, count=len(changes))
    if apply:
        if baseline_path is not None:
            save_baseline(baseline_path, pending.baseline())
        pending.apply()
        _emit_refused(writer, pending)
        return "confirmed"
    pending.discard()
    return "declined"


def _emit_refused(writer: EventWriter, pending: PendingChanges) -> None:
    """给不出基线、因此没落盘的改动要发一条 NOTE。

    静默扣成果与「报告说改好了而文件没变」是同一件事的两面——这条出口
    就是为了不让它发生。
    """
    refused = refused_note(pending)
    if refused:
        writer.emit(NOTE, text=refused, ok=False)


class HumanProgress:
    """把 AgentLoop 事件渲染成 stderr 进度行（chat / 散文 run 用）。

    不处理 `wait`：心跳已由 `WaitingGateway` + `WaitDisplay` 输出，再画一遍会叠字。
    `delta`：TTY 上用 Live 刷 Markdown 缓冲；非 TTY 追加纯文本。
    """

    def __init__(self) -> None:
        self._stream: _AssistantStream | None = None

    def handle(self, kind: str, data: dict) -> None:
        if kind in (DELTA, FINAL_DELTA):
            text = str(data.get("text") or "")
            if text:
                self._ensure_stream().feed(text)
            return
        if kind == SECTION_START:
            index = int(data.get("index") or 0) + 1
            total = data.get("total", "?")
            title = str(data.get("title") or "")
            print_err(f"· 第 {index}/{total} 节 {title}", markup=False, style="muted")
            return
        if kind == SECTION_DONE:
            text = str(data.get("text") or "")
            if text:
                self._ensure_stream().feed(text)
                if not text.endswith("\n"):
                    self._ensure_stream().feed("\n\n")
            return
        # 截断重试：保留已上屏正文与 Live，只打进度行。内核已跳过重复前缀，
        # 后续 delta 只会续写；若这里关面板，会清屏再从头刷一遍。
        if kind == NOTE and data.get("kind") == "truncation_retry":
            line = self._line(kind, data)
            if line:
                print_err(line, markup=False, style="muted")
            return
        # 其它事件到来时先收掉流式面板，避免与步骤行打架。
        self._close_stream()
        line = self._line(kind, data)
        if line:
            print_err(line, markup=False, style="muted")

    def finish(self) -> None:
        """回合结束：停 Live，留下已刷过的正文（终稿仍由 print_md 再印一份）。"""
        self._close_stream()

    def _ensure_stream(self) -> "_AssistantStream":
        if self._stream is None:
            self._stream = _AssistantStream()
        return self._stream

    def _close_stream(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    def _line(self, kind: str, data: dict) -> str:
        if kind == STEP:
            n = data.get("n", "?")
            return f"· 第 {n} 步"
        if kind == TOOL:
            name = str(data.get("name") or "工具")
            ok = bool(data.get("ok"))
            if data.get("no_checker"):
                status = "跳过（不是失败）"
            else:
                status = "成功" if ok else "失败"
            text = f"· {name} → {status}"
            detail = str(data.get("detail") or "").strip()
            if detail and (not ok or data.get("no_checker")):
                text = f"{text}（{detail}）"
            return text
        if kind == NOTE:
            text = str(data.get("text") or "").strip()
            if not text:
                return ""
            mark = "!" if data.get("ok") is False else "·"
            return f"{mark} {text}"
        if kind in (AWAIT, CONFIRM, DIFF, WAIT):
            return ""
        return ""


class _AssistantStream:
    """assistant delta → TTY Live Markdown，或非 TTY 追加。

    Live 用 ``transient=True``：回合结束停掉时清掉面板，终稿由 ``print_md``
    再印一份完整结论。截断重试不关面板（见 ``HumanProgress.handle``），由内核
    跳过重复前缀后只 ``feed`` 续写，避免整段重刷。
    """

    def __init__(self) -> None:
        from spoolkit.cli.term import CODE_THEME, console, set_stream_holds_tty

        self._buf = ""
        self._live = None
        self._plain = False
        con = console()
        if con.is_terminal and not con.no_color:
            from rich.live import Live
            from rich.markdown import Markdown

            # 先占住 TTY，停掉 WaitDisplay，避免加载符与正文互抢。
            set_stream_holds_tty(True)
            self._Markdown = Markdown
            self._code_theme = CODE_THEME
            self._live = Live(
                Markdown("", code_theme=CODE_THEME),
                console=con,
                refresh_per_second=8,
                transient=True,
            )
            self._live.start()
        else:
            self._plain = True

    def feed(self, piece: str) -> None:
        self._buf += piece
        if self._live is not None:
            self._live.update(
                self._Markdown(self._buf, code_theme=self._code_theme)
            )
        elif self._plain:
            from spoolkit.cli.term import console

            con = console()
            con.file.write(piece)
            con.file.flush()

    def close(self) -> None:
        from spoolkit.cli.term import set_stream_holds_tty

        if self._live is not None:
            self._live.stop()
            self._live = None
            set_stream_holds_tty(False)
        elif self._plain and self._buf:
            from spoolkit.cli.term import console

            con = console()
            con.file.write("\n")
            con.file.flush()
