"""P0 任务 8 的回滚演练（真跑，不是纸上）。

三步分别对应设计稿的"三步回滚"：

1. **文件层**：把 `memory.md.bak-<日期>` 改回 `memory.md`，断言逐字节等于
   迁移前的原文，且 `read_hot` 回到迁移前的内容；
2. **库层（旧表只读保留）**：在真实库的**副本**上，用改造前的建表脚本
   （`git show 240a6f2^:...` 的 `MEMORY_SCHEMA`）建一个旧库，断言 `memory`
   与 `memory_fts` 两个名字都还在、行数演练前后一致；
3. **索引层（按旧定义重建 FTS）**：把 v2 之后的库按旧定义
   `fts5(text)` 重建索引并按 `text` 回填，断言库恢复可读、`memory` 行数不变、
   `memory_fts` 又变回一列。

**不碰真实工作区**：第 2、3 步都只用 `.tmp/` 里的**副本**。
**不碰 `memory.py` 的判据**：这里只重建索引与改名，不改任何业务代码。
"""

import hashlib
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
REAL_DB = Path(r"D:\workSpace\agentWorkSpace\.agent\memory.db")
WORK = REPO / ".tmp" / "p0t8-rollback"

# 改造前（P0 任务 4/5 之前）的 store.py 版本：`240a6f2` 是"任务 4 与 5"的提交，
# 它的**父**提交就是改造前的代码。
BEFORE_REV = "240a6f2^"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def old_schema_source() -> str:
    """从 git 里取改造前的 `MEMORY_SCHEMA`（**不写文件**，直接读 stdout）。"""
    done = subprocess.run(
        ["git", "show", f"{BEFORE_REV}:src/spoolkit/memory/store.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if done.returncode != 0:
        raise SystemExit(f"取不到改造前的 store.py：{done.stderr}")
    return done.stdout


def old_memory_schema() -> str:
    source = old_schema_source()
    start = source.index('MEMORY_SCHEMA = """') + len('MEMORY_SCHEMA = """')
    end = source.index('"""', start)
    return source[start:end]


def step1_file_rollback() -> None:
    print("=== 步 1：文件层回滚（文件改名保留 → 还原） ===")
    ws = WORK / "ws"
    if ws.exists():
        shutil.rmtree(ws)
    (ws / ".agent").mkdir(parents=True)
    original = (ws / ".agent" / "memory.md")
    original.write_text(
        "# 项目记忆（自动维护，也可手动编辑）\n\n## 项目事实\n- 测试命令是 pytest -q\n- 你好！\n",
        encoding="utf-8",
    )
    before_bytes = original.read_bytes()
    before_sha = sha(original)
    print(f"  迁移前：{original.name} sha256={before_sha} bytes={len(before_bytes)}")

    done = subprocess.run(
        [sys.executable, "-m", "spoolkit.cli.app", "memory", "migrate", "--yes", "--root", str(ws)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=REPO,
    )
    print(f"  --yes 退出码={done.returncode}")
    backups = sorted((ws / ".agent").glob("memory.md.bak-*"))
    assert len(backups) == 1, backups
    backup = backups[0]
    print(f"  备份：{backup.name} sha256={sha(backup)}（与迁移前相同：{sha(backup) == before_sha}）")
    print(f"  迁移后 memory.md 内容：{original.read_text(encoding='utf-8')!r}")

    # 还原：先删掉迁移后新写的那一份，再把备份改回原名。
    # 这一步**必须显式**：`rename` 不覆盖已存在的目标文件（实测
    # `FileExistsError: WinError 183`），所以"备份改回原名"前面一定有一句
    # 删新文件的动作——回滚方案要写清这一点，不然第一次演练就会卡住。
    original.unlink()
    backup.rename(original)
    restored_sha = sha(original)
    print(f"  还原后：{original.name} sha256={restored_sha}")
    print(f"  逐字节等于迁移前：{restored_sha == before_sha}；字节数 {original.stat().st_size}")
    assert restored_sha == before_sha
    from spoolkit.memory.hot import read_hot

    print(f"  read_hot 还原结果：{read_hot(original)}")
    assert read_hot(original) == {"fact": ["测试命令是 pytest -q", "你好！"]}
    print("  步 1 通过：文件层完全可逆。\n")


def step2_old_tables_readable() -> None:
    print("=== 步 2：库层——旧表只读保留、旧代码照读 ===")
    WORK.mkdir(parents=True, exist_ok=True)
    copy = WORK / "real-copy.db"
    shutil.copy2(REAL_DB, copy)
    print(f"  真实库副本：{copy}（真实库 {REAL_DB} 未被触碰）")

    schema = old_memory_schema()
    old_db = WORK / "old.db"
    if old_db.exists():
        old_db.unlink()
    # 用本仓真正的可写入口建旧库：`store.db.open_db` 会设 `row_factory = Row`，
    # 而 `init_memory_schema` 是按名字读列的（裸 `sqlite3.connect` 不给
    # row_factory 会在 `PRAGMA table_info` 上炸成 `tuple indices must be
    # integers`）——演练里必须用真实入口，否则测的是夹具不是产品。
    from spoolkit.store.db import open_db

    conn = open_db(old_db)
    conn.executescript(schema)
    conn.execute(
        "INSERT INTO memory(kind, text, trigger, source, confidence, created_at)"
        " VALUES ('fact','测试命令是 pytest -q','','',0.5,1.0)"
    )
    conn.execute("INSERT INTO memory_fts(rowid, text) VALUES (1,'测试命令是 pytest -q')")
    conn.commit()
    version_before = conn.execute("PRAGMA user_version").fetchone()[0]
    rows_before = conn.execute("SELECT count(*) FROM memory").fetchone()[0]
    cols_before = [r[1] for r in conn.execute("PRAGMA table_info(memory_fts)")]
    print(f"  用改造前的 MEMORY_SCHEMA 建库：user_version={version_before}"
          f" memory 行数={rows_before} memory_fts 列={cols_before}")

    # 旧库走新版 init（等价于"新代码读过旧库"）：只加列、不删表、memory 行数不变。
    from spoolkit.memory import store

    store.init_memory_schema(conn)
    rows_after = conn.execute("SELECT count(*) FROM memory").fetchone()[0]
    names = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table')")
    }
    fts_ok = (
        conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='memory_fts'"
        ).fetchone()[0]
    )
    mem_ok = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='memory'"
    ).fetchone()[0]
    print(f"  init_memory_schema 后：user_version={conn.execute('PRAGMA user_version').fetchone()[0]}"
          f" memory 行数={rows_after}")
    print(f"  两个名字都还在：memory={('memory' in names)} memory_fts={('memory_fts' in names)}")
    print(f"  memory 建表语句仍与改造前同形：{'CREATE TABLE memory (' in mem_ok}")
    print(f"  memory_fts 现在是：{fts_ok.splitlines()[0]}")
    assert "memory" in names and "memory_fts" in names
    assert rows_after == rows_before

    # 旧代码照读的证明分两层（"旧代码"=改造前那份 store.py）：
    # ① 结构层：`memory` 建表语句与改造前**逐字相同**（上面已打印），旧代码
    #    的 `SELECT *` / 按 text 的 INSERT 都不会因为新列而炸；
    # ② 检索层：改造前写进 `memory_fts` 的那行是**原样文本**（没有逐字拆），
    #    而新版 init 会把索引按新定义**重建 + 回填**（回填走 Python 的逐字拆），
    #    所以升级后那行原样索引没了、换成规范化后的索引——这是设计稿 §9.3
    #    第 3 步"memory_fts 保留不删"的推论：**名字保留、内容是派生的**。
    #    这里就看"重建后还能不能查到"，不去假设旧索引行还在。
    hits = store.search_memories(conn, "测试命令")
    print(f"  重建索引后新检索路径读到：{[m.text for m in hits]}")
    assert [m.text for m in hits] == ["测试命令是 pytest -q"]
    print("  注意：改造前那行**原样文本**的索引被重建覆盖（索引是派生数据），"
          "底账 memory 一行未动。")
    conn.close()
    print("  步 2 通过：memory / memory_fts 只读保留、memory 行数不变、新检索照通。\n")


def step3_rebuild_old_fts() -> None:
    print("=== 步 3：按旧定义重建 FTS 并把库恢复到可读 ===")
    db = WORK / "rollback.db"
    if db.exists():
        db.unlink()
    shutil.copy2(REAL_DB, db)
    from spoolkit.memory import store
    from spoolkit.store.db import open_db

    conn = open_db(db)
    store.init_memory_schema(conn)
    user_version_upgraded = conn.execute("PRAGMA user_version").fetchone()[0]
    rows = conn.execute("SELECT count(*) FROM memory").fetchone()[0]
    cols_new = [r[1] for r in conn.execute("PRAGMA table_info(memory_fts)")]
    print(f"  升级后：user_version={user_version_upgraded} memory 行数={rows}"
          f" memory_fts 列={cols_new}")

    # 旧定义重建 + 按 text 回填（回滚动作）。
    conn.execute("DROP TABLE IF EXISTS memory_fts")
    conn.execute("CREATE VIRTUAL TABLE memory_fts USING fts5(text)")
    for row in conn.execute("SELECT id, text FROM memory").fetchall():
        conn.execute("INSERT INTO memory_fts(rowid, text) VALUES (?,?)", (row["id"], row["text"]))
    conn.commit()
    cols_old = [r[1] for r in conn.execute("PRAGMA table_info(memory_fts)")]
    rows_after = conn.execute("SELECT count(*) FROM memory").fetchone()[0]
    fts_rows = conn.execute("SELECT count(*) FROM memory_fts").fetchone()[0]
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    print(f"  回滚后：memory_fts 列={cols_old} memory 行数={rows_after} 索引行数={fts_rows}")
    print(f"  两个名字都还在：memory={'memory' in names} memory_fts={'memory_fts' in names}")
    # 库恢复可读：做一次真查询。
    probe = conn.execute("SELECT count(*) FROM memory").fetchone()[0]
    print(f"  库可读（SELECT count(*) FROM memory = {probe}）")
    assert cols_old == ["text"]
    assert rows_after == rows
    assert fts_rows == rows
    conn.close()
    print("  步 3 通过：索引可按旧定义重建，底账 memory 未被改动。\n")


def main() -> int:
    step1_file_rollback()
    step2_old_tables_readable()
    step3_rebuild_old_fts()
    print("回滚演练三步全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
