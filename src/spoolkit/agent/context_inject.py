"""把 ContextPack 渲染成可放进模型上下文的一块。

不读 artifact 正文，不改调用方的提示组装。空块保持原提示不动。
"""

from __future__ import annotations

from dataclasses import dataclass

from spoolkit.agent.context_pack import ContextPack, Kind
from spoolkit.llm.tokenizer import TokenCounter
from spoolkit.memory.transcript import redact_secrets

_HEADING = "【会话记忆】"
_LABELS: dict[Kind, str] = {
    "task": "当前任务",
    "open": "未完成",
    "decision": "决定",
    "artifact": "产物",
    "done": "已完成",
    "meta": "最近一轮",
}
_ARTIFACT_PREFIXES = ("title:", "summary:", "path:", "id:")


@dataclass(frozen=True, slots=True)
class ContextBlock:
    """一块会话记忆。text 为空时不注入。"""

    text: str
    tokens: int
    sections: tuple[str, ...]
    omitted: tuple[str, ...]


def build_context_block(pack: ContextPack, counter: TokenCounter, max_tokens: int) -> ContextBlock:
    """按 pack 里的节拼成记忆块。装不下的整节跳过，略过处只写数量。"""
    if max_tokens <= 0 or not pack.sections:
        return _empty()
    goal = redact_secrets(pack.goal.strip())
    prepared: list[tuple[Kind, str]] = []
    for section in pack.sections:
        lines = _lines(section.kind, section.lines, goal)
        if not lines:
            continue
        prepared.append((section.kind, _render_section(section.kind, lines)))
    if not prepared:
        return _empty()
    chosen: list[tuple[Kind, str]] = []
    skipped: list[str] = []
    for kind, rendered in prepared:
        trial = _render([item[1] for item in chosen] + [rendered], 0)
        if counter.count(trial) <= max_tokens:
            chosen.append((kind, rendered))
        else:
            skipped.append(kind)
    omitted = _merge(tuple(pack.omitted), skipped)
    while True:
        text = redact_secrets(_render([item[1] for item in chosen], len(omitted)))
        if counter.count(text) <= max_tokens and chosen:
            return ContextBlock(
                text=text,
                tokens=counter.count(text),
                sections=tuple(kind for kind, _rendered in chosen),
                omitted=omitted,
            )
        if not chosen:
            return _empty()
        kind, _rendered = chosen.pop()
        omitted = _merge(omitted, [kind])


def inject_context_block(prompt: str, block: ContextBlock) -> str:
    """空块不改提示。有内容时把记忆块放在提示前面。"""
    if not block.text:
        return prompt
    if not prompt:
        return block.text
    return f"{block.text}\n\n{prompt}"


def _empty() -> ContextBlock:
    return ContextBlock(text="", tokens=0, sections=(), omitted=())


def _lines(kind: Kind, lines: tuple[str, ...], goal: str) -> tuple[str, ...]:
    kept: list[str] = []
    for raw in lines:
        text = redact_secrets(str(raw).strip())
        if not text or text == goal or text in kept:
            continue
        if kind == "artifact" and not _artifact_line(text):
            continue
        kept.append(text)
    return tuple(kept)


def _artifact_line(text: str) -> bool:
    folded = text.casefold()
    return any(folded.startswith(prefix) for prefix in _ARTIFACT_PREFIXES)


def _render_section(kind: Kind, lines: tuple[str, ...]) -> str:
    body = "\n".join(f"- {line}" for line in lines)
    return f"{_LABELS[kind]}\n{body}"


def _render(parts: list[str], omitted: int) -> str:
    chunks = [_HEADING, *parts]
    if omitted:
        chunks.append(f"略过 {omitted} 项")
    return "\n".join(chunks)


def _merge(base: tuple[str, ...], extra: list[str]) -> tuple[str, ...]:
    merged = list(base)
    seen = set(base)
    for item in extra:
        if item not in seen:
            merged.append(item)
            seen.add(item)
    return tuple(merged)
