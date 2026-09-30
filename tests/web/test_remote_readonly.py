"""非回环绑定 = 只读（设计 §3.7）。

设计原话：网页壳默认绑 `127.0.0.1`；显式 `--host` 给了非 loopback 地址时，本批
**不实现远程可写**——`/run` 与 `/confirm` 一律 403，并说明"远程只能看"。

这里量五件事：

1. **判据是实际绑定的地址**，不是命令行那一层的那个字符串：`build_server` 绑完端口
   才定只读，所以测试或嵌入代码直接构造服务也拦得住（CLI 不是唯一入口，安全边界
   不能靠"大家都会走命令行"来成立）；
2. 只读拦的是**驱动**（`/run`、`/confirm`），不是**看**：页面、`/state`、`/events`
   照常——这条壳只读的意义就是"能看"；
3. 远程那一下**不落盘**：切视图允许（它只改进程内"在看谁"，不碰在跑的任务），
   但网页壳指针与工作区登记表都不写——那是这台机器上的持久状态，而本批没有任何
   鉴权来兜底；
4. 回环的几种写法照旧可写（`127.0.0.1` / `localhost`）。`::1` 只在判据的单测里：
   `ThreadingHTTPServer` 是 `AF_INET`，绑不上 IPv6 字面量；
5. 两条**服务端卫生**（写这块顺手踩出来的，都不是只读本身）：回话之前要把请求体
   读完、`/events` 收场要把连接关掉——两条都会以"几十秒后别的用例里冒出一段回溯"
   的形式发作。

不起长驻进程、不跑真 agent：服务绑临时端口（`port=0`）在后台线程里
`serve_forever`，`finally` 里关掉；`/run` 与 `/confirm` 用**不真起子进程**的假 Runner
（这条用例量的是闸门，不是任务跑得怎么样）。
"""

import http.client
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from spoolkit.kernel import pointers as kernel_pointers
from spoolkit.kernel.target import Target
from spoolkit.web.registry import RunnerRegistry
from spoolkit.web.runner import Runner
from spoolkit.web.server import (
    MAX_BODY,
    WebState,
    build_server,
    build_state,
    is_loopback_host,
)

#: 非回环绑定的代表：`0.0.0.0` 是"所有网卡"，凡是从别的机器来的都够得着。
#: 用它而不是本机的某个局域网地址（`192.168.x.x`）：那要求这台机器正好有一块
#: 那样的网卡，用例就会"在我这儿过、在你那儿红"。
REMOTE = "0.0.0.0"

#: 回环绑定的两种**能真绑上**的写法。`::1` 不在这里——见文件头的自述。
LOOPBACK = ("127.0.0.1", "localhost")


class _NoSpawn(Runner):
    """不真起子进程的假 Runner：只数"闸门有没有放它进来"。

    为什么不用真 agent：`/run` 被 403 拦住时**根本不该走到 Runner**，而这条正是
    要量的（拦在业务分支之前）。真起子进程反而会把断言绑到子进程的时序上。
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.started = 0
        self.confirmed = 0

    def start(self, goal: str) -> bool:
        self.started += 1
        return True

    def confirm(self, apply: bool) -> bool:
        self.confirmed += 1
        return True


@pytest.fixture
def start_server():
    """起一个真 HTTP 服务（端口交给系统），yield 一个 `start(state, host)`。

    **绑什么地址由调用方给**（`0.0.0.0` 那条是本文件的主角），而**连**一律走
    `127.0.0.1`：绑 `0.0.0.0` 的壳从本机也够得着，而"够得着"与"允许写"是两件事，
    这正是要量的那条边界。
    """
    made: list = []

    def start(state, host: str) -> str:
        httpd = build_server(state, host, 0)
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


def _gated_state(root: Path) -> WebState:
    """一个目标已经定好的机器级状态，Runner 换成不真起子进程的那个。

    目标要**先定好**：没有目标时 `/run` 本来就会 409，那样就分不清"被 403 拦住"
    和"还没有工作区"了——而这两种状态码正是本文件的判据。

    `--provider fake` 是刻意的：假模型的探活是瞬时的，而默认探活会去打本机模型
    地址（一两秒起步，还取决于这台机器上跑着什么）。
    """
    registry = RunnerRegistry(
        lambda item, name: _NoSpawn(
            item, session=name, extra_args=["--provider", "fake", "--script", ""]
        ),
        limit=1,
    )
    return WebState(
        registry,
        Target(
            root=root,
            session="s1",
            workspace_source="explicit",
            session_source="explicit",
        ),
        pinned=False,
    )


# --- 判据本身 ---


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "::1",
        "localhost",
        "[::1]",  # 照 URL 里的写法给（`--host [::1]`）
        "LOCALHOST",  # 大小写不该有意义
        "localhost.",  # 带根的 DNS 写法
        "127.0.0.2",  # 整个 127/8 都是回环，不只有 .0.1
    ],
)
def test_回环的几种写法都算回环(host: str) -> None:
    assert is_loopback_host(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",  # 所有网卡：正好是最该收紧的那一种
        "::",  # 同上，IPv6 写法
        "192.168.1.10",
        "10.0.0.1",
        "0.0.0.1",  # 不是回环（回环只有 127/8）
        "example.com",  # 认不出来的名字按远程办：这里宁可错杀
        "127.0.0.1:8765",  # 带端口不是地址，别当回环放过去
        "",
        "   ",
    ],
)
def test_非回环与认不出来的都按远程办(host: str) -> None:
    """判据的错法只有两种，这里只允许错成"更严"的那一种。

    "以为只是本机、其实全网可达"是不能接受的那种；反过来的代价是本地人也点不动
    那个按钮，一眼就能发现。
    """
    assert is_loopback_host(host) is False


def test_只读看的是实际绑上的地址(tmp_path: Path) -> None:
    """判据必须落在 `build_server` 里，而不是命令行那一层。

    `localhost` 到底落到 `127.0.0.1` 还是 `::1`、`0.0.0.0` 是不是真全网可达，
    只有绑完才知道；而测试与嵌入代码**直接构造服务**，绕开的正是 CLI。
    """
    root = _ws(tmp_path, "a")

    remote = _gated_state(root)
    httpd = build_server(remote, REMOTE, 0)
    try:
        assert remote.readonly() is True
        assert remote.bound_host() == REMOTE
    finally:
        httpd.server_close()
        remote.close()

    local = _gated_state(root)
    httpd = build_server(local, "localhost", 0)
    try:
        assert local.readonly() is False, "localhost 绑上之后是回环"
        assert local.bound_host() == "127.0.0.1", "记的必须是实际绑上的地址"
    finally:
        httpd.server_close()
        local.close()


# --- 回环：照旧可写 ---


@pytest.mark.parametrize("host", LOOPBACK)
def test_回环地址上照旧能驱动(start_server, tmp_path: Path, host: str) -> None:
    """默认那条路一个字都不许变：页面上的「开始」与「应用」还得能用。"""
    state = _gated_state(_ws(tmp_path, "a"))
    url = start_server(state, host)

    assert _state(url)["readonly"] is False

    status, body = _post(url + "/run", {"goal": "做点事"})
    assert status == 202, body
    status, body = _post(url + "/confirm", {"apply": True})
    assert status == 200, body

    runner = state.current()[1]
    assert runner.started == 1
    assert runner.confirmed == 1


# --- 非回环：驱动一律 403 ---


def test_非回环时run与confirm都是403(start_server, tmp_path: Path) -> None:
    state = _gated_state(_ws(tmp_path, "a"))
    url = start_server(state, REMOTE)

    for path, sent in (
        ("/run", {"goal": "做点事"}),
        ("/run", {}),  # 连 goal 都不给也是 403：判据排在业务分支**之前**
        ("/confirm", {"apply": True}),
    ):
        status, body = _post(url + path, sent)
        assert status == 403, f"{path} 竟然不是 403：{status} {body}"
        payload = json.loads(body)
        error = payload["error"]
        assert payload["readonly"] is True
        # 一句话要说全三件事：为什么、能看什么、想驱动怎么办（只说 403 会让人
        # 以为壳坏了，然后去把整个检查关掉）。
        assert "只能看" in error
        assert "非回环" in error
        assert "127.0.0.1" in error, "得给出路：本机开一个 / 绑回回环"

    runner = state.current()[1]
    assert (runner.started, runner.confirmed) == (0, 0), "403 之后还走到了 Runner"


def test_非回环时看的那部分照常(start_server, tmp_path: Path) -> None:
    """只读拦的是驱动，不是看：页面、`/state`、事件流都得照常工作。"""
    root = _ws(tmp_path, "a")
    state = build_state(
        None,
        pinned=False,
        target=Target(
            root=root,
            session="s1",
            workspace_source="explicit",
            session_source="default",
        ),
    )
    url = start_server(state, REMOTE)

    status, page = _get(url + "/")
    assert status == 200
    assert "<html" in page.lower()
    assert 'id="st-mode"' in page, "页面上得有那枚只读锁徽标（输入条那一行）"

    payload = _state(url)
    assert payload["readonly"] is True
    assert payload["host"] == REMOTE
    assert payload["target"]["root"] == str(root)

    with urllib.request.urlopen(url + "/events", timeout=10) as response:
        assert response.status == 200
        assert "text/event-stream" in response.headers["Content-Type"]
        first = response.readline().decode("utf-8")
    assert '"readonly": true' in first, "事件流的首帧也要说得出这条壳只读"


def test_非回环时切视图可以但不落盘(
    start_server, tmp_path: Path, monkeypatch
) -> None:
    """`POST /target` 的取舍：**允许切，但不写任何东西**。

    它改的是"这个进程现在看谁"，不驱动任何任务（在跑的那条不会被它碰到），所以
    拒掉它只会让远程连"看一眼别的工作区"都做不到——而那是这个功能存在的理由。
    但落盘是另一回事：网页壳指针会留到下次启动，工作区登记表会改本机的最近使用序，
    两者都是这台机器上的持久状态，而本批没有任何鉴权来兜底。线就划在
    "改进程内视图可以，改机器级持久状态不行"。
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("SPOOLKIT_STATE", str(state_dir))
    root = _ws(tmp_path, "a")
    state = build_state(None, pinned=False)
    url = start_server(state, REMOTE)

    status, body = _post(url + "/target", {"workspace": str(root)})

    assert status == 200, body
    payload = json.loads(body)
    assert payload["target"]["root"] == str(root), "切视图本身要生效"
    assert payload["readonly"] is True
    assert _state(url)["target"]["root"] == str(root)
    # 一个字节都没写：状态目录连文件都没造出来。
    assert list(state_dir.iterdir()) == [], "只读壳把远程那一下落了盘"
    assert kernel_pointers.get("web") is None


def test_回环时同一个动作照旧写指针(
    start_server, tmp_path: Path, monkeypatch
) -> None:
    """上一条的对照面：回环壳里同一个 `POST /target` 必须照旧写指针。

    没有这一条，"非回环不写"可以被写成"谁都不写"而全绿——那等于把刷新页面与
    重启服务都停不住这个功能悄悄删掉。
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("SPOOLKIT_STATE", str(state_dir))
    root = _ws(tmp_path, "a")
    state = build_state(None, pinned=False)
    url = start_server(state, "127.0.0.1")

    status, body = _post(url + "/target", {"workspace": str(root)})

    assert status == 200, body
    pointer = kernel_pointers.get("web")
    assert pointer is not None, "回环壳的显式切换也得写指针（重启服务要停得住）"
    assert pointer.workspace == root
    assert (state_dir / "clients.json").is_file()


# --- 收场时不许往 stderr 打回溯 ---


def test_事件流收场后不留回溯(start_server, tmp_path: Path, monkeypatch, capfd) -> None:
    """`/events` 的客户端走人之后，服务端**不许**往 stderr 打回溯。

    这条量的是一个真踩过的坑：客户端一走了之，服务端要到下一次写（心跳）才发现；
    那时内核已经收到 RST，基类接着去读"下一个请求"就在 `rfile.readline` 上抛
    `ConnectionAbortedError`，被 socketserver 打成一段回溯进 stderr：

        Exception occurred during processing of request from ('127.0.0.1', …)

    它出现在**十几秒之后、别的用例里**，看着与谁都不相干（本批真踩过：一个没人读的
    `/events` 在 20 秒后把回溯打进了 pytest 的 stderr，位置正好落在后面某个用例上，
    而 `scripts/pytest.ps1` 把子进程写 stderr 当致命错误——**整轮被掐掉**）。

    为什么要这么写（两个都是踩出来的）：

    - 心跳压到 0.05 秒、轮询也压到 0.05 秒：默认心脏 15 秒等不起；而错误真正浮出来
      还要内核那边一圈（实测**关掉之后约 2 秒**，与心跳周期无关）；
    - `--provider fake`：假模型的探活是瞬时的。默认探活会去打本机模型地址，
      首帧要 3 秒才到（实测），窗口就得开得更大、还依赖这台机器上跑着什么。

    证伪法：删掉 `_stream` 收场时那句 `self.close_connection = True`，这条变红。
    """
    from spoolkit.web import server as web_server

    monkeypatch.setattr(web_server, "KEEPALIVE_SECONDS", 0.05)
    monkeypatch.setattr(web_server, "TARGET_POLL_SECONDS", 0.05)
    state = _gated_state(_ws(tmp_path, "a"))
    url = start_server(state, "127.0.0.1")
    port = int(url.rsplit(":", 1)[1])

    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        sock.sendall(
            b"GET /events HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Accept: text/event-stream\r\n\r\n"
        )
        # 读到首帧为止（响应头和首帧不一定在同一个 TCP 段里）。
        buf = b""
        deadline = time.monotonic() + 10
        while b"state" not in buf and time.monotonic() < deadline:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        assert b"state" in buf, buf
    finally:
        # 走人。服务端下一次写就会把 RST 招回来——正是要制造的那个场面。
        # （**不用 SO_LINGER**：那个结构体在 Windows 与 POSIX 上大小不一样，
        # 为了测试去写平台分支不划算，普通 close 一样能把这条路径走出来。）
        sock.close()

    # 实测：关掉之后回溯**差不多立刻**就落地（错的实现里，第一次检查就看见）。
    # 给到 2.5 秒：一看见就停；没有回溯才是常态，等满就是白等。
    #
    # ⚠️ `readouterr()` 是**取走**（读一次就清空）：所以只读一次、存在变量里，
    # 不能循环里读一遍、断言时再读一遍——那样断言看到的永远是空串，用例永远绿
    # （这一版第一稿就是这么写的，变异验证当场把它逮住了）。
    err = ""
    for _ in range(25):
        err = capfd.readouterr().err
        if "Exception occurred" in err:
            break
        time.sleep(0.1)
    assert "Exception occurred" not in err, err


# --- 回话之前要把请求体读完 ---


def test_回话之前把请求体读完(start_server, tmp_path: Path) -> None:
    """403 也要把请求体读掉——**一条连接上连着发两个请求**就能看出来。

    为什么这条要单独量：没读完的接收数据有两个后果，而两个都不像"没读 body"：

    - keep-alive 时残留字节会被当成**下一个请求**的行，第二个响应变成 http.server
      自己发的 400/501（还写着 "Bad request syntax"，看起来像客户端发了坏包）；
    - 客户端说 `Connection: close` 时（urllib 默认如此），带着没读完的数据关 socket，
      Windows 发的是 RST 不是 FIN，客户端会在收那口响应时**随机**拿到
      `ConnectionAbortedError`。本批真踩过：单跑这个文件全绿，跑整个 `tests/web`
      红一条，看着与代码毫无关系——所以这里用**确定性**的写法钉住它。

    第二段走另一条分支（body 大过上限，服务端不收）：不收也**要读完**，否则同一个
    毛病换个入口还能复发。
    """
    small = json.dumps({"goal": "做点事"})

    # ① 只读壳的 403 那条路
    state = _gated_state(_ws(tmp_path, "a"))
    url = start_server(state, REMOTE)
    port = int(url.rsplit(":", 1)[1])
    remote = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        for _ in range(2):
            remote.request("POST", "/run", body=small,
                           headers={"Content-Type": "application/json"})
            response = remote.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            assert response.status == 403, payload
            assert payload["readonly"] is True
    finally:
        remote.close()

    # ② 太大而不收的 body（超过 MAX_BODY）：走回环壳，别被 403 抢先拦下
    big = json.dumps({"goal": "x" * (MAX_BODY + 1024)})
    local = _gated_state(_ws(tmp_path, "b"))
    url = start_server(local, "127.0.0.1")
    port = int(url.rsplit(":", 1)[1])
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("POST", "/run", body=big,
                     headers={"Content-Type": "application/json"})
        # 上一口的 body 要读掉，`http.client` 才会把这条连接交回给下一次请求
        # （不读的话它自己会报 ResponseNotReady——那是客户端的事，不是服务端的）。
        first = conn.getresponse()
        assert first.status == 400  # 太大 → 当成"没给 goal"
        first.read()
        conn.request("POST", "/run", body=small,
                     headers={"Content-Type": "application/json"})
        # 这一口必须是**我们发的**那一个（回环壳 → 202）。body 没读完的话，这里
        # 拿到的是 http.server 对着残字节发的 400/501。
        second = conn.getresponse()
        payload = json.loads(second.read().decode("utf-8"))
        assert second.status == 202, payload
        assert payload["ok"] is True
    finally:
        conn.close()


# --- 终端起手那行 ---


@pytest.mark.parametrize(
    ("host", "expected"),
    [("0.0.0.0", True), ("127.0.0.1", False)],
)
def test_起手那行就说清楚只能看(
    host: str, expected: bool, monkeypatch, capsys
) -> None:
    """别让人自己发现：绑了非回环地址，起手就要说。

    页面上那个「开始」点下去只会得到 403，而"为什么"不该等踩到才知道。
    这里真绑端口（判据要的就是实际地址）、但把 `serve_forever` 换成立刻返回：
    不起长驻进程，也不假装绑过。
    """
    from spoolkit.web import server as web_server

    real_build = web_server.build_server

    def build_but_do_not_loop(state, host_: str, port: int):
        httpd = real_build(state, host_, port)
        httpd.serve_forever = lambda: None
        return httpd

    monkeypatch.setattr(web_server, "build_server", build_but_do_not_loop)
    # 健康探针是个后台线程，会去打本机默认的模型地址——那会让用例的结果依赖
    # 这台机器上跑着什么（`test_target_endpoint.py` 的先例）。
    monkeypatch.setattr(web_server, "_watch_health", lambda *a, **k: None)

    web_server.serve(None, host=host, port=0, pinned=False)

    err = capsys.readouterr().err
    assert ("只能看" in err) is expected
    if expected:
        assert host in err, "还得说清绑在哪，人才知道从哪来的连接会被拦"
