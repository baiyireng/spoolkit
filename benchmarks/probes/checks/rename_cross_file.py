"""rename-cross-file 的验收判据。

「全项目都改过来」这句话是可以机械判定的，所以判据就照字面来：

1. 沙箱里（除 `.agent` / `__pycache__` 这些 harness 自己的目录）**任何**文本文件
   都不再出现旧的标识符；
2. 新名字真的在抓取层被定义了；
3. 项目自己的测试全过，并且**确实收集到了至少 4 条**——删掉测试文件也能让
   「0 tests, OK」退出码为 0，那不叫改完了；
4. 端到端跑一次命令行，合计数字仍然对。
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checker, child_env, iter_text_files, parse_args, run  # noqa: E402

OLD_NAME = "get_rows"
NEW_NAME = "load_rows"
MIN_TESTS_RUN = 4
EXPECTED_TOTAL = "合计 242.75"


def main():
    args = parse_args(__doc__)
    root = Path(args.root).resolve()
    checker = Checker("rename-cross-file")

    offenders = []
    for path in iter_text_files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        if OLD_NAME in text:
            hits = len(re.findall(re.escape(OLD_NAME), text))
            offenders.append(f"{path.relative_to(root)}（{hits} 处）")
    checker.require(
        not offenders,
        f"整个沙箱里没有 {OLD_NAME} 了（残留：{offenders}）",
    )

    fetch = root / "pipeline" / "fetch.py"
    if checker.require(fetch.is_file(), "pipeline/fetch.py 还在"):
        body = fetch.read_text(encoding="utf-8", errors="replace")
        checker.require(
            re.search(rf"^def {NEW_NAME}\b", body, flags=re.MULTILINE) is not None,
            f"{NEW_NAME} 在 pipeline/fetch.py 里被定义",
        )

    code, out, err, timed_out = run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=root,
        timeout=180,
        env=child_env(root),
    )
    checker.require(not timed_out, "项目测试没有挂死")
    checker.require(code == 0, f"项目自己的测试全过（退出码 {code}）")
    if code != 0:
        checker.note(f"  {' '.join((err or out).split())[-400:]}")
    match = re.search(r"Ran (\d+) tests?", (err or "") + (out or ""))
    ran = int(match.group(1)) if match else 0
    checker.require(
        ran >= MIN_TESTS_RUN,
        f"确实跑了 {MIN_TESTS_RUN} 条以上测试（实际 {ran} 条）",
    )

    code, out, err, _ = run(
        [sys.executable, "-m", "pipeline.cli", "data/parts.csv"],
        cwd=root,
        timeout=120,
        env=child_env(root),
    )
    checker.require(code == 0, "python -m pipeline.cli data/parts.csv 退出码 0")
    checker.require(EXPECTED_TOTAL in (out + err), f"端到端合计仍是 {EXPECTED_TOTAL}")

    return checker.finish()


if __name__ == "__main__":
    raise SystemExit(main())
