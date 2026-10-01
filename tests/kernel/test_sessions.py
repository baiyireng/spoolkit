"""会话：有生命周期，才有"一个工作区多个会话"这件事。"""
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from spoolkit.kernel import sessions


def _ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / ".agent").mkdir(parents=True)
    return root.resolve()


def test_新工作区还没有会话(tmp_path: Path) -> None:
    assert sessions.list_sessions(_ws(tmp_path)) == []


def test_建一条就能列出来(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    made = sessions.create_session(root, title="修 calc.py")
    listed = sessions.list_sessions(root)
    assert [item.id for item in listed] == [made]
    assert listed[0].title == "修 calc.py"


def test_标题从第一句目标截出来(tmp_path: Path) -> None:
    assert sessions.title_from_goal("  ") == ""
    assert sessions.title_from_goal("修好 sum_to") == "修好 sum_to"
    long_goal = "把" + "很长的目标" * 20
    assert len(sessions.title_from_goal(long_goal)) == sessions.TITLE_LIMIT


def test_第一句目标填标题之后不再覆盖(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    made = sessions.create_session(root)
    sessions.ensure_title(root, made, "第一句")
    sessions.ensure_title(root, made, "第二句")
    assert sessions.read_meta(root, made)["title"] == "第一句"


def test_改名(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    made = sessions.create_session(root, title="旧")
    sessions.rename_session(root, made, "新")
    assert sessions.list_sessions(root)[0].title == "新"


def test_归档只是从默认列表里藏起来(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    made = sessions.create_session(root, title="归档我")
    sessions.archive_session(root, made)
    assert sessions.list_sessions(root) == []
    assert [item.id for item in sessions.list_sessions(root, include_archived=True)] == [made]
    assert sessions.resolve_session(root, made) == made


def test_取消归档(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    made = sessions.create_session(root, title="来回")
    sessions.archive_session(root, made)
    sessions.archive_session(root, made, archived=False)
    assert [item.id for item in sessions.list_sessions(root)] == [made]


def test_按序号和latest解析(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    first = sessions.create_session(root, title="A")
    second = sessions.create_session(root, title="B")
    assert sessions.resolve_session(root, "latest") == second
    assert sessions.resolve_session(root, "1") == second  # 最近活跃在前
    assert sessions.resolve_session(root, "2") == first


def test_不认识的会话名要报错不要偷偷新建(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    first = sessions.create_session(root, title="A")
    with pytest.raises(sessions.SessionError) as raised:
        sessions.resolve_session(root, "查无此会话")
    assert first in str(raised.value)
    assert len(sessions.list_sessions(root)) == 1


def test_记忆库坏了也不炸(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    (root / ".agent" / "memory.db").write_text("这不是 sqlite", encoding="utf-8")
    assert sessions.list_sessions(root) == []


def test_撞名时不覆盖已有meta(tmp_path: Path) -> None:
    """先建一条，再拿同一个名字建：明确失败，盘上那份标题一个字不改。"""
    root = _ws(tmp_path)
    sessions.create_session(root, title="原来的标题", session_id="同名")

    with pytest.raises(sessions.SessionExists) as raised:
        sessions.create_session(root, title="后来的标题", session_id="同名")

    assert "同名" in str(raised.value)
    assert sessions.read_meta(root, "同名")["title"] == "原来的标题"


# --- 同名并发创建：窄竞态 -----------------------------------------------------
#
# 现象（改动前实测）：8 个线程同时拿同一个名字建会话，**8 个全都返回成功**
# （网页壳上就是都收到 200），而磁盘上只剩最后写的那份标题——人看到的是
# "我建的两条会话变成了一条"。根因是"先查再写"这条缝：`has_session` 与
# `write_meta` 之间，别人也能查到"没有"。
#
# 修法：点名那条走 `jsonstore.create_json`（`O_EXCL`），由内核裁决谁先创建。
# 输了的那一方拿到 `SessionExists`（网页壳照旧映成 409），绝不覆盖赢家的 meta。


def _widen_check_window(root: Path, count: int = 120) -> None:
    """先放一批别的会话，把"查过之后、写之前"那条缝拉宽。

    `has_session` 里那次 `list_sessions` 要真的读盘才有窗口；目录多了它就慢。
    这是把竞态变得**可稳定复现**的环境准备（不改产品代码），也是 `mutant` 版
    必须红的保证——否则一次调度巧合就可能蒙过去。
    """
    sessions_dir = root / ".agent" / "sessions"
    for index in range(count):
        item = sessions_dir / f"别的{index:03d}"
        item.mkdir(parents=True)
        (item / sessions.META_NAME).write_text(
            json.dumps({"id": item.name, "title": "x"}), encoding="utf-8"
        )


def test_同名并发创建_线程恰好一个成功(tmp_path: Path) -> None:
    """N 个线程抢同一个名字：恰好 1 个成功，其余 `SessionExists`，meta 是赢家的。

    证伪：把 `create_session` 里那次 `jsonstore.create_json` 换回 `write_meta`
    （先查再写），这里会变成 **N 个全成功**、且盘上标题属于最后写的那个——红。
    """
    root = _ws(tmp_path)
    _widen_check_window(root)
    name = "同名会话"
    workers = 8
    barrier = threading.Barrier(workers)
    successes: list[tuple[int, str]] = []
    refusals: list[str] = []
    lock = threading.Lock()

    def create(index: int) -> None:
        barrier.wait()
        try:
            made = sessions.create_session(root, title=f"标题{index}", session_id=name)
        except sessions.SessionExists as exc:
            with lock:
                refusals.append(str(exc))
        else:
            with lock:
                successes.append((index, made))

    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # 线程切得更勤：让这条缝真的被撞上
    try:
        threads = [threading.Thread(target=create, args=(i,)) for i in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sys.setswitchinterval(previous)

    assert len(successes) == 1, f"恰好一个该成功，实际 {successes}"
    assert len(refusals) == workers - 1
    assert all(name in message for message in refusals)
    # 磁盘上那份必须是**赢家**写的那份，不是"最后收尾的那个线程"
    winner_index, winner_id = successes[0]
    assert winner_id == name
    meta = sessions.read_meta(root, name)
    assert meta["title"] == f"标题{winner_index}"
    # 别的会话一个都没被牵连
    assert len(sessions.list_sessions(root, include_archived=True)) == 121


def test_同名并发创建_跨进程也只有一个成功(tmp_path: Path) -> None:
    """跨进程（CLI 一个、桥一个、hub 一个）同样只能有一个赢家。

    线程版证明的是"同一进程内那条缝"，这条证明的是"独占创建不靠进程内锁"——
    `O_EXCL` 是内核裁决，所以两个进程也只有一个能建成。
    """
    root = _ws(tmp_path)
    _widen_check_window(root)
    gate = tmp_path / "go"
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import sys, time\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from spoolkit.kernel import sessions\n"
        "root, gate, index = Path(sys.argv[2]), Path(sys.argv[3]), int(sys.argv[4])\n"
        # 先报到，再由父进程同时放行：起得慢的那个也会等到"大家都就位"。
        "(gate.parent / f'ready-{index}').write_text('1', encoding='utf-8')\n"
        "deadline = time.time() + 60\n"
        "while not gate.exists():\n"
        "    if time.time() > deadline:\n"
        "        print('TIMEOUT'); raise SystemExit(2)\n"
        "    time.sleep(0.002)\n"
        "try:\n"
        "    made = sessions.create_session(root, title=f'标题{index}',\n"
        "                                   session_id='同名会话')\n"
        "except sessions.SessionExists as exc:\n"
        "    print(f'NO {exc}')\n"
        "except Exception as exc:\n"
        "    print(f'NO-OTHER {type(exc).__name__}: {exc}')\n"
        "else:\n"
        "    print(f'OK 标题{index}')\n",
        encoding="utf-8",
    )
    source = str(Path(sessions.__file__).resolve().parents[2])
    workers = 6

    procs = [
        subprocess.Popen(
            [sys.executable, str(worker), source, str(root), str(gate), str(index)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        for index in range(workers)
    ]
    deadline = time.time() + 60
    while not all((tmp_path / f"ready-{i}").exists() for i in range(workers)):
        assert time.time() < deadline, "有 worker 没起来"
        time.sleep(0.01)
    gate.write_text("go", encoding="utf-8")

    outs = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=120)
        outs.append((stdout.strip(), stderr.strip()))

    ok = [text for text, _ in outs if text.startswith("OK")]
    no = [text for text, _ in outs if text.startswith("NO ")]
    assert not [text for text, _ in outs if text.startswith("NO-OTHER")], outs
    assert len(ok) == 1, outs
    assert len(no) == len(procs) - 1, outs
    assert all("同名会话" in text for text in no)

    winner_title = ok[0].removeprefix("OK ").strip()
    assert sessions.read_meta(root, "同名会话")["title"] == winner_title

