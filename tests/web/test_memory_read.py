"""当前会话的产物、摘要、checkpoint 只读接口。不写盘，不交正文。"""

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

from spoolkit.agent.artifacts import ArtifactRecord, ArtifactSection, ArtifactStore
from spoolkit.agent.session_summary import (
    SCHEMA_VERSION,
    SessionSummary,
    SessionSummaryStore,
    SummaryArtifact,
)
from spoolkit.web.memory_read import (
    ARTIFACT_LIST_MAX,
    artifact_limit,
    read_artifacts,
)
from spoolkit.web.runner import Runner
from spoolkit.web.server import build_server

BODY = "ARTIFACT-BODY-LEAK"
SECRET = "API_KEY=sk-test-secret-value"
REDACTED = "API_KEY=***"
CHECKPOINT_BODY = "CHECKPOINT-BODY-LEAK"
STATE_TEXT = "STATE-SHOULD-NOT-LEAK"
OTHER = "别的会话独有"


def _put(root: Path, session: str, artifact_id: str, title: str, created_at: str, body: str) -> None:
    ArtifactStore(root).put(
        ArtifactRecord(
            id=artifact_id,
            session=session,
            task_id="task-1",
            turn_id="turn-1",
            kind="document",
            title=title,
            summary="只有摘要",
            created_at=created_at,
            sections=(
                ArtifactSection(index=0, title="窗口", summary="节摘要", char_count=len(body)),
            ),
        ),
        body,
    )


def _checkpoint(
    session: str,
    key: str,
    updated_at: str,
    goal: str = "把估算写完",
    path: str = "docs/estimate.md",
    deliverable: str = "file",
) -> dict:
    return {
        "schema_version": 1,
        "session": session,
        "checkpoint_key": key,
        "artifact_id": "doc-turn-1",
        "task_id": "task-1",
        "turn_id": "turn-1",
        "phase": "stopped",
        "status": "paused",
        "updated_at": updated_at,
        "state": {
            "goal": goal,
            "path": path,
            "deliverable": deliverable,
            "error": STATE_TEXT,
        },
        "bodies": {"0": CHECKPOINT_BODY},
    }


def _files(root: Path) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            found[path.relative_to(root).as_posix()] = path.read_bytes()
    return found


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _post(url: str, payload: dict) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _serve(root: Path, session: str, host: str = "127.0.0.1"):
    runner = Runner(root, session=session, extra_args=["--provider", "fake", "--script", ""])
    httpd = build_server(runner, host, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, f"http://127.0.0.1:{httpd.server_port}"


def test_limit只认数字并被压住(tmp_path: Path, monkeypatch) -> None:
    assert artifact_limit("/artifacts") is None
    assert artifact_limit("/artifacts?path=../sess-b&session=sess-b&limit=1") == 1
    assert artifact_limit("/artifacts?limit=../../sess-b") is None
    seen: dict[str, int] = {}
    real = ArtifactStore.list

    def spy(self, session: str, limit: int):
        seen["limit"] = limit
        return real(self, session, limit)

    monkeypatch.setattr(ArtifactStore, "list", spy)
    assert read_artifacts(tmp_path, "sess-a", limit=10**9) == []
    assert seen["limit"] == ARTIFACT_LIST_MAX
    assert read_artifacts(tmp_path, "") == []
    assert read_artifacts(tmp_path, "  ") == []
    assert "limit" in seen
    assert not (tmp_path / ".agent").exists()


def test_空session与缺文件是空结果(tmp_path: Path) -> None:
    httpd, url = _serve(tmp_path, "")
    try:
        status, artifacts = _get(url + "/artifacts")
        assert status == 200
        assert json.loads(artifacts) == []
        status, summary = _get(url + "/session/summary")
        assert status == 200
        payload = json.loads(summary)
        assert payload["session"] == ""
        assert payload["headline"] == ""
        assert payload["what_done"] == []
        assert payload["artifacts"] == []
        status, checkpoints = _get(url + "/checkpoints")
        assert status == 200
        assert json.loads(checkpoints) == []
        assert not (tmp_path / ".agent").exists()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_三个接口只读当前session且不含正文(tmp_path: Path) -> None:
    _put(tmp_path, "sess-a", "doc-old", "旧稿", "2026-01-01T00:00:00Z", BODY + "-old")
    _put(tmp_path, "sess-a", "doc-mid", "中稿", "2026-01-02T00:00:00Z", BODY + "-mid")
    _put(tmp_path, "sess-a", "doc-new", "新稿", "2026-01-03T00:00:00Z", BODY)
    _put(tmp_path, "sess-b", "doc-other", OTHER, "2026-01-04T00:00:00Z", BODY + "-other")
    SessionSummaryStore(tmp_path, "sess-a").save(
        SessionSummary(
            schema_version=SCHEMA_VERSION,
            session="sess-a",
            updated_at="2026-01-03T00:00:00Z",
            headline="写过估算",
            what_done=("写完第一节",),
            artifacts=(
                SummaryArtifact(id="doc-new", title="新稿", summary="摘要引用", path=""),
            ),
            last_turn_id="turn-1",
        )
    )
    folder = tmp_path / ".agent" / "checkpoints" / "sess-a"
    folder.mkdir(parents=True)
    (folder / "ck-new.json").write_text(
        json.dumps(_checkpoint("sess-a", "ck-new", "2026-01-03T00:00:00Z"), ensure_ascii=False),
        encoding="utf-8",
    )
    (folder / "ck-old.json").write_text(
        json.dumps(_checkpoint("sess-a", "ck-old", "2026-01-01T00:00:00Z"), ensure_ascii=False),
        encoding="utf-8",
    )
    (folder / "broken.json").write_text("{", encoding="utf-8")
    (folder / "ck-schema.json").write_text(
        json.dumps({"schema_version": 2, "session": "sess-a", "checkpoint_key": "ck-schema"}),
        encoding="utf-8",
    )
    nested = folder / "nested"
    nested.mkdir()
    (nested / "ck-hidden.json").write_text(
        json.dumps(_checkpoint("sess-a", "ck-hidden", "2026-01-04T00:00:00Z"), ensure_ascii=False),
        encoding="utf-8",
    )
    other = tmp_path / ".agent" / "checkpoints" / "sess-b"
    other.mkdir(parents=True)
    (other / "ck-other.json").write_text(
        json.dumps(_checkpoint("sess-b", "ck-other", "2026-01-04T00:00:00Z"), ensure_ascii=False),
        encoding="utf-8",
    )
    before = _files(tmp_path)
    httpd, url = _serve(tmp_path, "sess-a")
    try:
        status, listed = _get(url + "/artifacts")
        assert status == 200
        artifacts = json.loads(listed)
        assert [item["id"] for item in artifacts] == ["doc-new", "doc-mid", "doc-old"]
        assert set(artifacts[0]) == {
            "id",
            "title",
            "summary",
            "path",
            "kind",
            "created_at",
            "sections",
        }
        assert artifacts[0]["title"] == "新稿"
        assert artifacts[0]["summary"] == "只有摘要"
        assert artifacts[0]["kind"] == "document"
        assert artifacts[0]["sections"] == [
            {"index": 0, "title": "窗口", "summary": "节摘要", "char_count": len(BODY)}
        ]
        assert BODY not in listed
        assert OTHER not in listed

        status, limited = _get(url + "/artifacts?path=../sess-b&session=sess-b&limit=1")
        assert status == 200
        assert [item["id"] for item in json.loads(limited)] == ["doc-new"]
        assert OTHER not in limited
        assert BODY not in limited

        status, summary_body = _get(url + "/session/summary?path=../sess-b/summary.json")
        assert status == 200
        summary = json.loads(summary_body)
        assert summary["session"] == "sess-a"
        assert summary["headline"] == "写过估算"
        assert summary["what_done"] == ["写完第一节"]
        assert summary["artifacts"] == [
            {"id": "doc-new", "title": "新稿", "summary": "摘要引用", "path": ""}
        ]
        assert BODY not in summary_body
        assert CHECKPOINT_BODY not in summary_body

        status, checkpoint_body = _get(url + "/checkpoints?session=sess-b&path=../sess-b")
        assert status == 200
        checkpoints = json.loads(checkpoint_body)
        assert [item["checkpoint_key"] for item in checkpoints] == ["ck-new", "ck-old"]
        assert set(checkpoints[0]) == {
            "checkpoint_key",
            "artifact_id",
            "task_id",
            "turn_id",
            "phase",
            "status",
            "updated_at",
            "goal",
            "path",
            "deliverable",
            "title",
        }
        assert checkpoints[0]["artifact_id"] == "doc-turn-1"
        assert checkpoints[0]["status"] == "paused"
        assert checkpoints[0]["phase"] == "stopped"
        assert checkpoints[0]["goal"] == "把估算写完"
        assert checkpoints[0]["path"] == "docs/estimate.md"
        assert checkpoints[0]["deliverable"] == "file"
        assert checkpoints[0]["title"] == "把估算写完"
        assert "state" not in checkpoints[0]
        assert "bodies" not in checkpoints[0]
        assert CHECKPOINT_BODY not in checkpoint_body
        assert STATE_TEXT not in checkpoint_body
        assert "ck-other" not in checkpoint_body
        assert "ck-hidden" not in checkpoint_body

        for path in ("/artifacts", "/session/summary", "/checkpoints"):
            status, denied = _post(url + path, {"path": "../sess-b", "session": "sess-b"})
            assert status == 404
            assert json.loads(denied) == {"error": "没有这个路径"}
        status, missing = _get(url + "/artifacts/../checkpoints")
        assert status == 404
        assert _files(tmp_path) == before
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_没有摘要时返回空摘要且坏checkpoint留在盘上(tmp_path: Path) -> None:
    folder = tmp_path / ".agent" / "checkpoints" / "sess-a"
    folder.mkdir(parents=True)
    broken = folder / "broken.json"
    broken.write_text("{", encoding="utf-8")
    before = _files(tmp_path)
    httpd, url = _serve(tmp_path, "sess-a")
    try:
        status, artifacts = _get(url + "/artifacts")
        assert status == 200
        assert json.loads(artifacts) == []
        status, summary_body = _get(url + "/session/summary")
        assert status == 200
        summary = json.loads(summary_body)
        assert summary["session"] == "sess-a"
        assert summary["headline"] == ""
        assert summary["artifacts"] == []
        assert summary["what_done"] == []
        status, checkpoints = _get(url + "/checkpoints")
        assert status == 200
        assert json.loads(checkpoints) == []
        assert _files(tmp_path) == before
        assert broken.read_text(encoding="utf-8") == "{"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_产物元数据里的密钥只在返回时脱敏(tmp_path: Path) -> None:
    meta_path = tmp_path / ".agent" / "artifacts" / "sess-a" / "doc-secret.json"
    meta_path.parent.mkdir(parents=True)
    meta_path.write_text(
        json.dumps(
            {
                "id": "doc-secret",
                "session": "sess-a",
                "task_id": "task-1",
                "turn_id": "turn-1",
                "kind": "document",
                "title": f"标题 {SECRET}",
                "summary": f"摘要 {SECRET}",
                "path": f"docs/{SECRET}.md",
                "created_at": "2026-01-01T00:00:00Z",
                "content": BODY,
                "sections": [
                    {
                        "index": 0,
                        "title": f"节标题 {SECRET}",
                        "summary": f"节摘要 {SECRET}",
                        "char_count": 4,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    summary_path = tmp_path / ".agent" / "sessions" / "sess-a" / "summary.json"
    summary_path.parent.mkdir(parents=True)
    summary_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "session": "sess-a",
                "updated_at": "2026-01-01T00:00:00Z",
                "headline": f"进度 {SECRET}",
                "what_done": [],
                "artifacts": [
                    {
                        "id": "doc-secret",
                        "title": f"标题 {SECRET}",
                        "summary": f"摘要 {SECRET}",
                        "path": f"docs/{SECRET}.md",
                    }
                ],
                "decisions": [],
                "open_items": [],
                "last_turn_id": "",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    meta_before = meta_path.read_bytes()
    summary_before = summary_path.read_bytes()
    before = _files(tmp_path)
    httpd, url = _serve(tmp_path, "sess-a")
    try:
        status, listed = _get(url + "/artifacts")
        assert status == 200
        item = json.loads(listed)[0]
        assert item["title"] == f"标题 {REDACTED}"
        assert item["summary"] == f"摘要 {REDACTED}"
        assert item["path"] == f"docs/{REDACTED}"
        assert item["sections"] == [
            {
                "index": 0,
                "title": f"节标题 {REDACTED}",
                "summary": f"节摘要 {REDACTED}",
                "char_count": 4,
            }
        ]
        assert SECRET not in listed
        assert REDACTED in listed
        assert BODY not in listed
        status, summary_body = _get(url + "/session/summary")
        assert status == 200
        summary = json.loads(summary_body)
        assert summary["headline"] == f"进度 {REDACTED}"
        assert summary["artifacts"] == [
            {
                "id": "doc-secret",
                "title": f"标题 {REDACTED}",
                "summary": f"摘要 {REDACTED}",
                "path": f"docs/{REDACTED}",
            }
        ]
        assert SECRET not in summary_body
        assert meta_path.read_bytes() == meta_before
        assert summary_path.read_bytes() == summary_before
        assert SECRET.encode("utf-8") in meta_before
        assert _files(tmp_path) == before
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_checkpoint列表给出可续写字段并脱敏(tmp_path: Path) -> None:
    """goal、path、deliverable 和标题来自 state。正文和密钥不出现在返回里。"""
    long_goal = "写完这一节" * 10
    folder = tmp_path / ".agent" / "checkpoints" / "sess-a"
    folder.mkdir(parents=True)
    long_path = folder / "ck-long.json"
    long_path.write_text(
        json.dumps(
            _checkpoint("sess-a", "ck-long", "2026-01-03T00:00:00Z", goal=long_goal),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    secret_path = folder / "ck-secret.json"
    secret_path.write_text(
        json.dumps(
            _checkpoint(
                "sess-a",
                "ck-secret",
                "2026-01-02T00:00:00Z",
                goal=f"目标 {SECRET} 后续",
                path=f"docs/{SECRET}.md",
                deliverable=f"file {SECRET}",
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (folder / "ck-plain.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session": "sess-a",
                "checkpoint_key": "ck-plain",
                "artifact_id": "doc-turn-1",
                "task_id": "task-1",
                "turn_id": "turn-1",
                "phase": "draft",
                "status": "running",
                "updated_at": "2026-01-01T00:00:00Z",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (folder / "broken.json").write_text("{", encoding="utf-8")
    before = _files(tmp_path)
    httpd, url = _serve(tmp_path, "sess-a")
    try:
        status, body = _get(url + "/checkpoints")
        assert status == 200
        rows = json.loads(body)
        assert [item["checkpoint_key"] for item in rows] == ["ck-long", "ck-secret", "ck-plain"]
        assert rows[0]["goal"] == long_goal
        assert rows[0]["title"] == long_goal[:40]
        assert len(rows[0]["title"]) == 40
        assert rows[1]["goal"] == "目标 API_KEY=*** 后续"
        assert rows[1]["title"] == "目标 API_KEY=*** 后续"
        assert rows[1]["path"] == "docs/API_KEY=***"
        assert rows[1]["deliverable"] == "file API_KEY=***"
        assert SECRET not in body
        assert CHECKPOINT_BODY not in body
        assert STATE_TEXT not in body
        assert "state" not in rows[1]
        assert "bodies" not in rows[1]
        assert rows[2]["goal"] == ""
        assert rows[2]["path"] == ""
        assert rows[2]["deliverable"] == ""
        assert rows[2]["title"] == ""
        assert _files(tmp_path) == before
        assert secret_path.read_bytes() == before[secret_path.relative_to(tmp_path).as_posix()]
        assert SECRET.encode("utf-8") in secret_path.read_bytes()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_只读壳仍能看这三个接口(tmp_path: Path) -> None:
    _put(tmp_path, "sess-a", "doc-new", "新稿", "2026-01-03T00:00:00Z", BODY)
    before = _files(tmp_path)
    httpd, url = _serve(tmp_path, "sess-a", host="0.0.0.0")
    try:
        status, artifacts = _get(url + "/artifacts")
        assert status == 200
        assert json.loads(artifacts)[0]["id"] == "doc-new"
        assert BODY not in artifacts
        status, summary_body = _get(url + "/session/summary")
        assert status == 200
        assert json.loads(summary_body)["session"] == "sess-a"
        status, checkpoints = _get(url + "/checkpoints")
        assert status == 200
        assert json.loads(checkpoints) == []
        status, denied = _post(url + "/run", {"goal": "不要写"})
        assert status == 403
        assert _files(tmp_path) == before
    finally:
        httpd.shutdown()
        httpd.server_close()
