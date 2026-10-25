"""跨会话任务历史：只读 `.agent/task-events/*.jsonl`，不画界面。"""

import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path

from spoolkit.kernel.sessions import write_meta
from spoolkit.web.runner import Runner
from spoolkit.web.server import build_server
from spoolkit.web.task_history import read_task_history


def _line(task_id: str, summary: str, **extra) -> str:
    payload = {
        "task_id": task_id,
        "run_id": "run-1",
        "turn_id": "turn-1",
        "seq": 1,
        "step": 0,
        "phase": "understand",
        "source": "system",
        "status": "running",
        "summary": summary,
        "tool": "",
        "refs": [],
        "artifacts": [],
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _events(root: Path) -> Path:
    path = root / ".agent" / "task-events"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _stamp(path: Path, seconds: int) -> None:
    os.utime(path, ns=(seconds * 1_000_000_000, seconds * 1_000_000_000))


def test_两个会话分组带session且坏行不进目录(tmp_path: Path) -> None:
    events = _events(tmp_path)
    alpha = events / "alpha.jsonl"
    alpha.write_text(
        "\n".join(
            [
                "这行不是 JSON",
                _line(
                    "shared",
                    "旧目标",
                    raw="RAW_SECRET",
                    thought="秘密思考",
                ),
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    beta = events / "beta.jsonl"
    beta.write_text(
        "\n".join(
            [
                _line("early", "先写的目标"),
                "{",
                _line("shared", "后写的目标"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _stamp(alpha, 1_000)
    _stamp(beta, 2_000)
    write_meta(tmp_path, "beta", title="后写的会话")

    catalog = read_task_history(tmp_path)

    assert catalog["sessions"] == [
        {"id": "beta", "title": "后写的会话"},
        {"id": "alpha", "title": "alpha"},
    ]
    assert [item["goal"] for item in catalog["groups"]] == [
        "后写的目标",
        "先写的目标",
        "旧目标",
    ]
    assert [(item["session"], item["task_id"]) for item in catalog["groups"]] == [
        ("beta", "shared"),
        ("beta", "early"),
        ("alpha", "shared"),
    ]
    packed = json.dumps(catalog, ensure_ascii=False)
    assert "RAW_SECRET" not in packed
    assert "秘密思考" not in packed
    assert "raw" not in catalog["groups"][-1]["events"][0]
    assert "thought" not in catalog["groups"][-1]["events"][0]
    assert str(tmp_path) not in packed
    assert ".agent" not in packed


def test_目录外和非jsonl不读(tmp_path: Path) -> None:
    events = _events(tmp_path)
    (events / "keep.jsonl").write_text(_line("keep", "留下") + "\n", encoding="utf-8")
    (events / "notes.txt").write_text(_line("txt", "文本") + "\n", encoding="utf-8")
    nested = events / "nested"
    nested.mkdir()
    (nested / "hidden.jsonl").write_text(_line("hidden", "嵌套") + "\n", encoding="utf-8")
    outside = tmp_path / "outside.jsonl"
    outside.write_text(_line("out", "外面") + "\n", encoding="utf-8")
    link = events / "link.jsonl"
    try:
        link.symlink_to(outside)
    except OSError:
        link = None

    catalog = read_task_history(tmp_path)

    assert [item["id"] for item in catalog["sessions"]] == ["keep"]
    assert [item["goal"] for item in catalog["groups"]] == ["留下"]
    if link is not None:
        assert link.is_symlink()


def test_读不了的文件跳过其余照常(tmp_path: Path, monkeypatch) -> None:
    events = _events(tmp_path)
    (events / "ok.jsonl").write_text(_line("ok", "还能读") + "\n", encoding="utf-8")
    (events / "locked.jsonl").write_text(_line("locked", "锁住") + "\n", encoding="utf-8")
    real = Path.read_text

    def _read(self: Path, *args, **kwargs):
        if self.name == "locked.jsonl":
            raise OSError(13, "拒绝访问")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _read)

    catalog = read_task_history(tmp_path)

    assert [item["id"] for item in catalog["sessions"]] == ["ok"]
    assert [item["goal"] for item in catalog["groups"]] == ["还能读"]


def test_没有事件目录时是空目录(tmp_path: Path) -> None:
    assert read_task_history(tmp_path) == {"sessions": [], "groups": []}


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


def test_接口只读且不改当前快照(tmp_path: Path) -> None:
    runner = Runner(
        tmp_path,
        session="t",
        extra_args=["--provider", "fake", "--script", ""],
    )
    events = _events(tmp_path)
    path = events / "alpha.jsonl"
    body = _line("alpha", "磁盘上的目标", raw="RAW_SECRET") + "\n"
    path.write_text(body, encoding="utf-8")
    before = path.read_bytes()
    httpd = build_server(runner, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_port}"
    try:
        status, state_body = _get(url + "/state")
        assert status == 200
        state_before = json.loads(state_body)
        status, tasks_body = _get(url + "/tasks")
        assert status == 200
        catalog = json.loads(tasks_body)
        assert catalog["sessions"] == [{"id": "alpha", "title": "alpha"}]
        assert catalog["groups"][0]["session"] == "alpha"
        assert catalog["groups"][0]["goal"] == "磁盘上的目标"
        assert "RAW_SECRET" not in tasks_body
        status, state_after_body = _get(url + "/state")
        assert status == 200
        state_after = json.loads(state_after_body)
        assert set(state_before) == set(state_after)
        assert "groups" not in state_after
        assert state_before["task_groups"] == state_after["task_groups"] == []
        assert path.read_bytes() == before
        status, denied = _post(url + "/tasks", {"path": "../"})
        assert status == 404
        assert path.read_bytes() == before
        status, missing = _get(url + "/tasks?path=../")
        assert status == 404
        assert json.loads(missing) == {"error": "没有这个路径"}
    finally:
        httpd.shutdown()
        httpd.server_close()
