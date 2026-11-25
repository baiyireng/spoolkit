"""`POST /session/new`：顶部那个「＋ 新建会话」终于有后端了。

改之前页面上那个按钮只做"把目标切到一个还没跑过的新名字"，而 `/target` **不新建**
会话（`resolve_session` 不认识就报错、且绝不静默新建）——点下去只有 400。

这一批补的是**真的建会话**那条路（复用既有的 `kernel_sessions.create_session`），
这里量七件事：

1. 建出来的会话**出现在 `/state` 的 `sessions` 里**（不必等它跑过一轮才出现）；
2. 建完能**切过去**，名字与存储位置都是既有那套（`.agent/sessions/<id>/meta.json`）；
3. **撞名**被拒（409）并说人话——`meta.json` 会被静默覆盖，而记忆库里那一行还在；
4. **名字非法**（空串 / 路径分隔符 / 超长 / 会被 `safe()` 改写的那些）被拒（400）
   并说人话，不冒栈；
5. **不写指针**：建一条会话不等于"选了它"（"显式选择才写指针"是既有契约），
   随后那次 `POST /target` 才写；
6. 非回环绑定一律 **403**：与 `/run`、`/session/delete` 走**同一张** `WRITE_PATHS` 表
   ——新的写端点漏出安全边界是这一批最容易犯的错；
7. 还没有当前目标时 409 说人话。

不起长驻进程、不跑真 agent：服务绑临时端口（`port=0`）在后台线程里
`serve_forever`，`finally` 里关掉。
"""

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from spoolkit.kernel import pointers as kernel_pointers
from spoolkit.kernel import sessions as kernel_sessions
from spoolkit.kernel.target import Target
from spoolkit.web.server import build_server, build_state


@pytest.fixture
def start_server():
    made: list = []

    def start(state) -> str:
        httpd = build_server(state, "127.0.0.1", 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        made.append((httpd, thread, state))
        return f"http://127.0.0.1:{httpd.server_port}"

    yield start
    for httpd, thread, state in made:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        state.close()


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


def _state(url: str) -> dict:
    status, body = _get(url + "/state")
    assert status == 200, body
    return json.loads(body)


def _ws(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    (root / ".agent").mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _state_for(root: Path, session: str = "s1", *, pinned: bool = False):
    return build_state(
        None,
        pinned=pinned,
        target=Target(
            root=root,
            session=session,
            workspace_source="explicit",
            session_source="explicit",
        ),
        extra_args=["--provider", "fake", "--script", ""],
    )


# --- ① 建出来、看得见、切得过去 ---


def test_新建的会话出现在状态里并切得过去(start_server, tmp_path: Path) -> None:
    root = _ws(tmp_path, "a")
    state = _state_for(root, "s1")
    url = start_server(state)
    assert _state(url)["sessions"] == []

    status, body = _post(url + "/session/new", {"session": "网页建的一条"})

    assert status == 200, body
    made = json.loads(body)["session"]
    assert made == "网页建的一条", "服务端没按点名的名字建（或悄悄改写了它）"
    # ① 出现在 /state 的 sessions 里（不必等它跑过一轮）
    rows = _state(url)["sessions"]
    assert [row["id"] for row in rows] == ["网页建的一条"]
    assert rows[0]["current"] is False, "建出来不等于切过去了"

    # ② 切过去：走既有的那条路（/target），而且它就认这个名字
    status, body = _post(url + "/target", {"session": made})
    assert status == 200, body
    payload = json.loads(body)
    assert payload["target"]["session"] == made
    assert payload["target"]["root"] == str(root), "建会话不该动工作区"

    # 存储位置照既有约定（`kernel/sessions.py::meta_path`）
    assert kernel_sessions.meta_path(root, made).is_file()
    listed = [item.id for item in kernel_sessions.list_sessions(root)]
    assert made in listed


def test_不点名就照既有约定自动生成id(start_server, tmp_path: Path) -> None:
    """`spool session new` 那条路逐字保留：id 是"时间戳-两位随机"。"""
    root = _ws(tmp_path, "a")
    url = start_server(_state_for(root))

    status, body = _post(url + "/session/new", {})

    assert status == 200, body
    made = json.loads(body)["session"]
    assert made != ""
    assert kernel_sessions.meta_path(root, made).is_file()
    assert made in [row["id"] for row in _state(url)["sessions"]]


def test_标题照传并出现在列表里(start_server, tmp_path: Path) -> None:
    """与 `spool session new <标题>` 同一个口径：标题是给人认的旁路信息。"""
    root = _ws(tmp_path, "a")
    url = start_server(_state_for(root))

    status, body = _post(url + "/session/new", {"session": "web-1", "title": "改缓存"})

    assert status == 200, body
    assert json.loads(body)["title"] == "改缓存"
    row = [item for item in _state(url)["sessions"] if item["id"] == "web-1"][0]
    assert row["title"] == "改缓存"


# --- ② 撞名与非法名都要说人话 ---


def test_撞名被拒且说人话(start_server, tmp_path: Path) -> None:
    """撞名不许静默覆盖：`meta.json` 会被写掉，而记忆库那一行还在。

    证伪法：把 `create_session` 里那次 `has_session` 判断删掉，这条会变成 200
    并且**覆盖**掉原来那条会话的标题。
    """
    root = _ws(tmp_path, "a")
    first = kernel_sessions.create_session(root, title="先有的这条")
    url = start_server(_state_for(root, first))

    status, body = _post(url + "/session/new", {"session": first})

    assert status == 409, body
    payload = json.loads(body)
    assert first in payload["error"]
    assert "Traceback" not in body
    # 原来那一条一个字都没被动过，也没有多出第二条。
    assert kernel_sessions.read_meta(root, first)["title"] == "先有的这条"
    assert len([row for row in _state(url)["sessions"] if row["id"] == first]) == 1


def test_已归档的名字也算撞名(start_server, tmp_path: Path) -> None:
    """归档只是从默认列表里藏起来：那一条的目录与记忆都还在，不能在上面"新建"。"""
    root = _ws(tmp_path, "a")
    gone = kernel_sessions.create_session(root, title="归档过的那条")
    kernel_sessions.archive_session(root, gone)
    url = start_server(_state_for(root, "s1"))

    status, body = _post(url + "/session/new", {"session": gone})

    assert status == 409, body
    assert gone in json.loads(body)["error"]


@pytest.mark.parametrize(
    ("name", "why"),
    [
        ("   ", "空串（只有空白）"),
        ("a/b", "路径分隔符"),
        ("a\\b", "Windows 路径分隔符"),
        ("a b", "空格——`safe()` 会把它写成下划线，两条活就共用一个目录了"),
        ("x" * 49, "超长"),
        ("结尾点.", "`safe()` 会去掉首尾的点"),
        ("_开头", "`safe()` 会去掉首位的下划线"),
        ("冒号:也不许", "别的符号"),
    ],
)
def test_名字非法一律四百并说人话(start_server, tmp_path: Path, name, why) -> None:
    root = _ws(tmp_path, "a")
    url = start_server(_state_for(root, "s1"))

    status, body = _post(url + "/session/new", {"session": name})

    assert status == 400, f"{why} 居然被收下了：{body}"
    assert json.loads(body)["error"], "拒绝了却不说为什么"
    assert "Traceback" not in body
    assert _state(url)["sessions"] == [], "被拒之后不该留下半条会话"


def test_给了空名字与没给名字是两件事(start_server, tmp_path: Path) -> None:
    """`{"session": ""}` 是**给了一个空名字**（400），`{}` 才是"你替我起一个"。"""
    root = _ws(tmp_path, "a")
    url = start_server(_state_for(root, "s1"))

    status, body = _post(url + "/session/new", {"session": ""})
    assert status == 400, body
    assert "空" in json.loads(body)["error"]

    assert _post(url + "/session/new", {})[0] == 200


def test_太长那个数字说得出上限(start_server, tmp_path: Path) -> None:
    root = _ws(tmp_path, "a")
    url = start_server(_state_for(root, "s1"))

    status, body = _post(url + "/session/new", {"session": "x" * 200})

    assert status == 400, body
    assert str(kernel_sessions.NAME_LIMIT) in json.loads(body)["error"]


# --- ③ 指针：显式选择才写 ---


def test_建会话不写指针切过去才写(start_server, tmp_path: Path) -> None:
    """"显式选择才写指针"是既有契约（`WebState::_remember` 的自述）。

    建一条会话**不等于选了它**——页面接着发的那次 `POST /target` 才是。
    证伪法：在 `create_session` 里加一句 `_remember(...)`，第一段就红。
    """
    root = _ws(tmp_path, "a")
    url = start_server(_state_for(root, "s1"))

    assert _post(url + "/session/new", {"session": "web-1"})[0] == 200
    assert kernel_pointers.get("web") is None, "建了一条会话却记成了「人选了它」"

    assert _post(url + "/target", {"session": "web-1"})[0] == 200
    pointer = kernel_pointers.get("web")
    assert pointer is not None and pointer.session == "web-1"
    assert pointer.workspace == root


# --- ④ 还没有目标 / 只读 ---


def test_没有目标时新建被拒给人话(start_server) -> None:
    url = start_server(build_state(None, pinned=False))

    status, body = _post(url + "/session/new", {"session": "web-1"})

    assert status == 409, body
    assert "工作区" in json.loads(body)["error"]
    assert "Traceback" not in body


def test_非回环绑定下一律403(start_server, tmp_path: Path) -> None:
    """新写端点必须进同一张 `WRITE_PATHS` 表：漏一个就是把安全边界漏在门外。

    证伪法：把 `/session/new` 从 `WRITE_PATHS` 里拿掉，这条立刻变成 200 并在
    磁盘上真的建出一条会话。
    """
    root = _ws(tmp_path, "a")
    state = _state_for(root, "s1")
    url = start_server(state)  # 回环那个壳（用来对照）
    remote = build_server(state, "0.0.0.0", 0)
    thread = threading.Thread(target=remote.serve_forever, daemon=True)
    thread.start()
    remote_url = f"http://127.0.0.1:{remote.server_port}"
    try:
        status, body = _post(remote_url + "/session/new", {"session": "远程想建的"})
        assert status == 403, body
        payload = json.loads(body)
        assert payload["readonly"] is True
        assert "只能看" in payload["error"]
        # 只读壳上**照旧能看**（这正是"只读"的意义）。
        assert _get(remote_url + "/state")[0] == 200
    finally:
        remote.shutdown()
        remote.server_close()
        thread.join(timeout=5)

    # 远程那一下一个字都没落盘：既没有这条会话，也没有任何会话。
    assert _state(url)["sessions"] == []
    assert not kernel_sessions.meta_path(root, "远程想建的").exists()


def test_钉住的壳照样能建只是切不过去(start_server, tmp_path: Path) -> None:
    """`--root` 钉的是"看哪一条"，不是"什么都不能改"（与 `/session/delete` 一个口径）。

    建得出来；但页面上那个「＋」在钉住的壳里本来就是禁用的，而真要切过去时
    `/target` 会给出 409 与理由。
    """
    root = _ws(tmp_path, "a")
    url = start_server(_state_for(root, "s1", pinned=True))

    status, body = _post(url + "/session/new", {"session": "钉住时建的"})

    assert status == 200, body
    assert "钉住时建的" in [row["id"] for row in _state(url)["sessions"]]

    status, body = _post(url + "/target", {"session": "钉住时建的"})
    assert status == 409, body
    assert "--root" in json.loads(body)["error"]
