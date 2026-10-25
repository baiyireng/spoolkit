"""vague-optimize 的验收判据。

用户只说「太慢了，别改坏结果」，所以判据就落在这两件事上，多一条都不许加：

1. **结果逐字不变**：判据自己用一份独立的参考实现算出摘要（不调被测模块），
   逐字段比对。参考实现只看文件内容，所以「少读一半」「只算前 N 行」这类
   偷工减料会立刻露出来。
2. **真的快了**：在同一台机器上对同一份输入计时，必须低于阈值。输入由判据
   自己在沙箱**外面**生成，模型改不了它。

阈值取的是种子的 1/3 与修好版本的 20 倍之间的中间地带，两边都留着大余量，
不靠「差不多的计时」判定。
"""

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checker  # noqa: E402
from _common import parse_args  # noqa: E402

# 判据自己造的输入规模。种子的实现在这个规模上要好几秒，修好的实现零点几秒。
LOG_LINES = 30000
TIME_LIMIT_SECONDS = 4.0
TOP_LIMIT = 5

HOT = ["/api/items", "/api/items/", "/api/search", "/static/app.js", "/", "/login"]
HOT_SHARE = 0.15
TAIL_ID_RANGE = 2_000_000


def build_log(path, count):
    """造一份和种子里 gen_log.py 同形状的日志：少数热路径 + 很长的长尾。

    长尾是关键——种子的实现在热路径上几乎不花时间（第一次就命中），
    慢的全在长尾上（每次都把已有结果从头扫到尾，而且扫完发现没有）。
    """
    import random

    rng = random.Random(20250312)
    lines = []
    for index in range(count):
        if rng.random() < HOT_SHARE:
            path_hit = rng.choice(HOT)
        else:
            path_hit = f"/api/items/{rng.randint(1, TAIL_ID_RANGE)}"
        code = rng.choice([200, 200, 200, 200, 304, 404, 500, 301])
        lines.append(
            f'10.0.3.11 - - [12/Mar/2025:10:{index % 60:02d}:{index % 60:02d} +0800] '
            f'"GET {path_hit} HTTP/1.1" {code} {rng.randint(120, 9000)}'
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return count


def reference_summary(text):
    """独立参考实现：只按种子说明里的格式约定解析，不 import 被测模块。"""
    pairs = []
    for line in text.splitlines():
        parts = line.split('"')
        if len(parts) < 3:
            continue
        request = parts[1].split()
        if len(request) < 2:
            continue
        tail = parts[2].split()
        if len(tail) < 2:
            continue
        try:
            code = int(tail[0])
        except ValueError:
            continue
        pairs.append((request[1], code))

    codes = {}
    order = []
    counts = {}
    for path, code in pairs:
        codes[code] = codes.get(code, 0) + 1
        if path not in counts:
            counts[path] = 0
            order.append(path)
        counts[path] += 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return {
        "total": len(pairs),
        "codes": codes,
        "top_paths": [[path, hits] for path, hits in ranked[:TOP_LIMIT]],
    }


def main():
    args = parse_args(__doc__)
    root = Path(args.root).resolve()
    checker = Checker("vague-optimize")

    sys.path.insert(0, str(root))
    for name in [key for key in list(sys.modules) if key == "logstat" or key.startswith("logstat.")]:
        del sys.modules[name]

    try:
        from logstat import report as subject
    except Exception as exc:  # noqa: BLE001 - 导入失败也是判据不成立
        checker.require(False, f"logstat.report 能导入（实际：{exc!r}）")
        return checker.finish()

    checker.require(hasattr(subject, "summarize"), "logstat.report.summarize 还在")

    # 输入落在沙箱**外面**的一个临时文件里，模型碰不到它。
    #
    # 刻意不用 `tempfile.TemporaryDirectory`：本机沙箱里它清理时会
    # `chmod(0o700)` 那个目录并被拒（PermissionError），判据还没跑就崩了。
    # 直接建文件、直接 unlink，不碰目录权限。
    handle, name = tempfile.mkstemp(prefix="probe-vague-optimize-", suffix=".log")
    os.close(handle)
    log_path = Path(name)
    try:
        build_log(log_path, LOG_LINES)
        raw = log_path.read_text(encoding="utf-8")
        expected = reference_summary(raw)
        checker.note(f"输入 {LOG_LINES} 行，参考摘要 total={expected['total']}")

        started = time.perf_counter()
        try:
            actual = subject.summarize(log_path)
        except Exception as exc:  # noqa: BLE001
            checker.require(False, f"summarize() 跑得通（实际：{exc!r}）")
            return checker.finish()
        elapsed = time.perf_counter() - started
    finally:
        try:
            os.unlink(log_path)
        except OSError:  # pragma: no cover - 删不掉不影响判定
            pass

    checker.note(f"summarize() 实测 {elapsed:.2f}s（阈值 {TIME_LIMIT_SECONDS:.2f}s）")

    got = dict(actual or {})
    checker.require(got.get("total") == expected["total"], "total 与参考一致")
    checker.require(got.get("codes") == expected["codes"], "codes 与参考一致")
    got_top = [list(item) for item in (got.get("top_paths") or [])]
    checker.require(got_top == expected["top_paths"], "top_paths 的次序与参考一致")
    if got_top != expected["top_paths"]:
        checker.note(f"  参考 {expected['top_paths']}")
        checker.note(f"  实际 {got_top}")

    checker.require(
        elapsed < TIME_LIMIT_SECONDS,
        f"耗时低于阈值（{elapsed:.2f}s < {TIME_LIMIT_SECONDS:.2f}s）",
    )

    try:
        rendered = subject.render(actual)
        checker.require("总请求数" in rendered, "render() 仍能渲染摘要")
    except Exception as exc:  # noqa: BLE001
        checker.require(False, f"render() 跑得通（实际：{exc!r}）")

    return checker.finish()


if __name__ == "__main__":
    raise SystemExit(main())
