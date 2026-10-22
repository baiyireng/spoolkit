"""对已经落盘的快照重跑一遍七条判定，把结果写回快照。

为什么需要它：`scripts/interact_probe.py` 里的判据会随取证调整（比如把
"等你确认"里命令授权那一条排出去），而快照里的 `judge` 字段是**生成那一刻**
的结果——不重判就会拿着旧判据的结论去写报告。

    python scripts/rejudge_snapshots.py            # 重判 .probe_runs 下全部快照
    python scripts/rejudge_snapshots.py a.json b.json

只动 `judge` 一个字段，事件流/工具回话/产物一个字不改。
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import interact_probe as ip  # noqa: E402


def main(argv: list[str]) -> int:
    targets = [Path(item) for item in argv] or sorted(
        (REPO / ip.RUNS_DIRNAME).glob("snapshot-*.json")
    )
    if not targets:
        print("没有找到快照")
        return 1
    for path in targets:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        snapshot["judge"] = ip.judge(snapshot)
        path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"== {path.name}")
        for row in snapshot["judge"]:
            print(f"  {row['id']}. "
                  f"{'复现' if row['reproduced'] else '没看到'}  {row['title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
