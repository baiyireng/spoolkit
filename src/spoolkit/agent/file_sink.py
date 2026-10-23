"""长文文件输出。全文进 artifact，工作区目标只登记待确认修改。

不调用 PendingChanges.apply，不写目标文件，也不发 TaskEvent。
节引用指向 artifact；文档自己的 path 是待确认的目标路径。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from spoolkit.agent.artifacts import ArtifactRecord, ArtifactSection, ArtifactStore
from spoolkit.agent.document_state import SUMMARY_CAP, CompletedSection, DocumentState
from spoolkit.errors import PathOutsideProjectError
from spoolkit.tools.edit import PendingChange, PendingChanges


class FileSinkError(ValueError):
    """路径不成立，或还不能提交全文。"""


class FileSink:
    """按节收正文，完成时一次写入 artifact 并 propose 一处修改。"""

    def __init__(
        self,
        state: DocumentState,
        path: str,
        session: str,
        task_id: str,
        turn_id: str,
        artifacts: ArtifactStore,
        pending: PendingChanges,
        artifact_id: str,
    ) -> None:
        if state.deliverable not in ("file", "both"):
            raise FileSinkError("FileSink 只接受 file 或 both")
        relative = _workspace_relative(pending, path)
        if _workspace_relative(pending, state.path) != relative:
            raise FileSinkError("目标路径与文档路径不一致")
        self.state = state
        self.path = relative
        self.session = session
        self.task_id = task_id
        self.turn_id = turn_id
        self.artifacts = artifacts
        self.artifact_id = artifact_id
        self._pending = pending
        self.pending: PendingChange | None = None
        self.record: ArtifactRecord | None = None
        self.reasons: tuple[str, ...] = ()
        self._bodies: dict[int, str] = {}
        self._done = False
        self._failed = False

    def start(self, state: DocumentState) -> None:
        """文档开始。不写文件。"""
        self.state = state

    def section_start(self, index: int, title: str) -> None:
        """开始一节。正文还没提交。"""
        del index, title

    def section_commit(self, index: int, body: str) -> None:
        """记下这一节的最终正文。不写目标文件。"""
        self._remember(index, body)

    def section_replace(self, index: int, body: str) -> None:
        """替换这一节的最终正文。不写目标文件。"""
        self._remember(index, body)

    def progress(self, phase: str, index: int) -> None:
        del phase, index

    def complete(self, result: object) -> None:
        """拼全文、写入 artifact，并登记一处待确认修改。"""
        del result
        if self._done:
            return
        if self._failed:
            raise FileSinkError("失败后不能再提交")
        text = self._render()
        record = self.artifacts.put(self._record(text), text)
        self.record = record
        self.pending = self._pending.propose(self.path, text)
        self._done = True

    def fail(self, reasons: tuple[str, ...]) -> None:
        """不写目标文件，也不登记待确认修改。"""
        self._failed = True
        self.reasons = tuple(reasons)

    def section_refs(self) -> tuple[CompletedSection, ...]:
        """已完成节只指向 artifact，不带正文。"""
        if self.record is None:
            raise FileSinkError("还没有 artifact")
        return tuple(
            CompletedSection(
                index=item.index,
                title=item.title,
                summary=item.summary,
                artifact_id=self.record.id,
                path=self.record.path,
                char_count=item.char_count,
            )
            for item in self.record.sections
        )

    def _remember(self, index: int, body: str) -> None:
        if self._done or self._failed:
            return
        if index not in {item.index for item in self.state.outline}:
            raise FileSinkError("节不在大纲里")
        self._bodies[index] = body

    def _render(self) -> str:
        parts: list[str] = []
        for entry in self.state.outline:
            body = self._bodies.get(entry.index, "").strip()
            if not body:
                raise FileSinkError("节正文不齐")
            parts.append(f"## {entry.title}\n\n{body}")
        return "\n\n".join(parts)

    def _record(self, text: str) -> ArtifactRecord:
        del text
        sections: list[ArtifactSection] = []
        for entry in self.state.outline:
            body = self._bodies[entry.index].strip()
            sections.append(
                ArtifactSection(
                    index=entry.index,
                    title=entry.title,
                    summary=_section_summary(body),
                    char_count=len(body),
                )
            )
        return ArtifactRecord(
            id=self.artifact_id,
            session=self.session,
            task_id=self.task_id,
            turn_id=self.turn_id,
            kind="document",
            title=_clip(self.state.outline[0].title if self.state.outline else self.state.goal),
            summary=_clip(self.state.goal),
            created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            sections=tuple(sections),
        )


def _section_summary(body: str) -> str:
    """节摘要取正文本身，最多 240 字。不把整节正文放进元数据。"""
    compact = "".join(line.strip() for line in body.splitlines() if line.strip())
    return _clip(compact)


def _clip(text: str) -> str:
    if len(text) <= SUMMARY_CAP:
        return text
    return text[: SUMMARY_CAP - 1] + "…"


def _workspace_relative(pending: PendingChanges, path: str) -> str:
    try:
        relative = pending.path_of(path)
    except PathOutsideProjectError as exc:
        message = "路径为空" if not str(path).strip() else "路径越界"
        raise FileSinkError(message) from exc
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise FileSinkError("路径越界")
    return relative
