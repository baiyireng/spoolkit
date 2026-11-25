"""P0 的自检（只读，不产生真机读数）。

跑法：

    .venv\\Scripts\\python.exe benchmarks\\probes\\orchestration\\selfcheck_probe_fields.py

它验四件事（都在跑分器的**纯函数**上，不起进程、不碰 8080）：

1. `concluded` / `terminated_by` 从 FINAL 事件里读得到；
2. 旧事件流（没有这两个字段）退回「模型自认完成」而不是猜；
3. 主循环没报终止者时，跑分器按自己能看见的事实补（超时 / 启动失败 /
   进程异常退出 / 模型完成 / 步数上限）；
4. 跳过记录里也有这两个字段（口径是「跳过」，不是「失败」也不是「步数上限」）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "src"))
sys.path.insert(0, str(HERE.parents[2] / "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import probe  # noqa: E402


def _line(payload: dict) -> str:
    return json.dumps({"type": "final", **payload}, ensure_ascii=False)


def main() -> int:
    ok = True

    # ① 新事件流：两个字段都读得到。
    counters = probe.parse_events(
        _line(
            {
                "ok": False,
                "text": "报告正文",
                "concluded": False,
                "terminated_by": "步数上限",
                "failed_reason": "",
                "model_final": "",
            }
        )
    )
    assert counters["concluded"] is False
    assert counters["terminated_by"] == "步数上限"
    assert probe.derive_terminated_by(counters, {"exit_code": 1}) == "步数上限"

    # ② 旧事件流：没有 concluded 字段时退回 final_ok。
    old = probe.parse_events(_line({"ok": True, "text": "做完了"}))
    assert old["concluded"] is True, "旧事件流里模型自认完成应当算有结论"
    assert probe.derive_terminated_by(old, {"exit_code": 0}) == "模型完成"

    # ③ 跑分器自己补的那几档。
    empty = probe.parse_events("")
    assert probe.derive_terminated_by(empty, {"timed_out": True}) == "超时"
    assert probe.derive_terminated_by(empty, {"spawn_error": "x"}) == "失败"
    assert (
        probe.derive_terminated_by(empty, {"exit_code": 3})
        == "失败"
    ), "进程异常退出且没有 final：那是死了，不是没做完"
    assert probe.derive_terminated_by(empty, {"exit_code": 1}) == "步数上限"

    # ④ 跳过记录。
    skipped = probe.skipped_record(
        {
            "id": "x",
            "shape": "s",
            "title": "t",
            "goal": "g",
            "seed": "seeds/x",
        },
        {"temperature": 0, "seed": None, "rotate_variants": True},
        0,
        1,
        100,
        100,
    )
    assert skipped["terminated_by"] == "跳过"
    assert skipped["concluded"] is False
    assert skipped["failed_reason"] == ""

    # ⑤ kind=run_failed 的 note 能按 kind 数出来（跑分器不靠正则）。
    note = json.dumps(
        {
            "type": "note",
            "text": "运行失败（模型/分词这一步）：LlamaCppError: tokenize 返回 502",
            "ok": False,
            "kind": "run_failed",
            "error_type": "LlamaCppError",
        },
        ensure_ascii=False,
    )
    failed = probe.parse_events(
        note
        + "\n"
        + _line(
            {
                "ok": False,
                "text": "⚠ 本次运行失败",
                "concluded": False,
                "terminated_by": "失败",
                "failed_reason": "LlamaCppError: tokenize 返回 502",
            }
        )
    )
    assert failed["terminated_by"] == "失败"
    assert "tokenize 返回 502" in failed["failed_reason"]

    print("ok" if ok else "fail")
    print("① concluded / terminated_by 读得到")
    print("② 旧事件流退回「模型自认完成」")
    print("③ 超时 / 启动失败 / 异常退出 / 完成 / 步数上限 五档都补得上")
    print("④ 跳过记录带 terminated_by=跳过")
    print("⑤ run_failed 那条 note 与 FINAL 上的 failed_reason 都读得到")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
