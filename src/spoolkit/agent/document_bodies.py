"""长文正文的存放处。第一版只放内存，文件和 artifact 留到 L4。"""

from __future__ import annotations


class BodyStoreError(Exception):
    """正文仓库拒绝了这次读写。"""


def section_ref(index: int) -> str:
    """一节在内存仓库里的稳定引用。正文不进 DocumentState。"""
    return f"section-{index}"


class InMemoryBodyStore:
    """按节下标存放正文。put 返回引用，get / update / exists 都用这个引用。"""

    def __init__(self) -> None:
        self._bodies: dict[str, str] = {}

    def put(self, index: int, body: str) -> str:
        if index < 0:
            raise BodyStoreError("下标为负")
        ref = section_ref(index)
        if ref in self._bodies:
            raise BodyStoreError("该节已有正文")
        self._bodies[ref] = body
        return ref

    def get(self, ref: str) -> str:
        try:
            return self._bodies[ref]
        except KeyError as exc:
            raise BodyStoreError("找不到正文") from exc

    def update(self, ref: str, body: str) -> None:
        if ref not in self._bodies:
            raise BodyStoreError("找不到正文")
        self._bodies[ref] = body

    def exists(self, ref: str) -> bool:
        return ref in self._bodies
