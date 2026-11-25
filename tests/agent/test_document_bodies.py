"""正文在 BodyStore 里，不进 DocumentState。"""

import pytest

from spoolkit.agent.document_bodies import BodyStoreError, InMemoryBodyStore, section_ref
from spoolkit.agent.document_state import DocumentState, OutlineEntry


def test_memory_store_put_get_update_exists() -> None:
    store = InMemoryBodyStore()
    body = "甲" * 500 + "。"
    ref = store.put(0, body)
    assert ref == section_ref(0)
    assert store.exists(ref)
    assert store.get(ref) == body
    store.update(ref, body + "续。")
    assert store.get(ref).endswith("续。")
    assert not store.exists(section_ref(1))
    with pytest.raises(BodyStoreError):
        store.get(section_ref(1))
    with pytest.raises(BodyStoreError):
        store.update(section_ref(1), "没有这一节")
    with pytest.raises(BodyStoreError):
        store.put(0, "重复写入")


def test_body_stays_out_of_document_state() -> None:
    store = InMemoryBodyStore()
    body = "SENTINEL_BODY" + ("窗口写完。" * 40)
    store.put(0, body)
    state = DocumentState(
        goal="写一份估算说明",
        outline=(OutlineEntry(0, "窗口", "窗口的要点", 500),),
        deliverable="chat",
        phase="plan",
    )
    assert body not in state.to_json()
    assert "SENTINEL_BODY" not in state.to_json()
