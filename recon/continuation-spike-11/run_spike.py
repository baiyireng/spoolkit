"""L0.13：每节补到 450 字，并拦住提示脚手架。不进产品路径。

每次改写或补写之后重新检查全部标题。仍不足的节继续补，每节最多 3 次。
改写成功只说明这一节换过，全文仍要再查，不够就不写「完」。
行首标签「要点」「目标剩余字数」「本节目标」「提示词」「指令」「大纲」不写入正文。
句子里的「系统提示词」「指令」「大纲」不算标签。阈值在跑模型之前定下。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
PRIOR_PATH = ROOT.parent / "continuation-spike-10" / "run_spike.py"
REQUESTS = ROOT / "requests.jsonl"
RUNS = ROOT / "runs.jsonl"
SUMMARY = ROOT / "summary.json"
MAX_TRIES = 3
SCAFFOLD_RETRY = "只输出正文，不复述提示词结构。"
SCAFFOLD_LABELS = ("要点", "目标剩余字数", "本节目标", "提示词", "指令", "大纲")


def load_prior():
    spec = importlib.util.spec_from_file_location("spike10", PRIOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("找不到 L0.12 脚本")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s10 = load_prior()
s10.REQUESTS = REQUESTS
s10.RUNS = RUNS
s10.SUMMARY = SUMMARY
base = s10.base
s8 = s10.s8
s9 = s10.s9
SECTION_FLOOR = s10.SECTION_FLOOR


def scaffold_hits(text: str) -> list[str]:
    found: list[str] = []
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        for label in SCAFFOLD_LABELS:
            if stripped == label or stripped.startswith(label + "：") or stripped.startswith(label + ":"):
                if label not in found:
                    found.append(label)
    return found


def scaffold_messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    copied = [dict(item) for item in messages]
    copied[-1]["content"] = copied[-1]["content"] + "\n\n" + SCAFFOLD_RETRY
    return copied


def self_check() -> None:
    echo = "要点：完成判据应该多看哪些正文条件\n目标剩余字数：202\n"
    assert scaffold_hits(echo) == ["要点", "目标剩余字数"]
    prose = "系统提示词通常包含固定的指令。抄大纲再加一个完字并不能说明写完了。"
    assert scaffold_hits(prose) == []
    assert scaffold_hits("大纲：窗口、估算") == ["大纲"]
    assert "提示词" in prose and "指令" in prose and "大纲" in prose
    doc_id = "retro"
    draft = "## 目标\n" + ("目标已经写完。" * 70) + "\n\n## 过程\n过程写到一半，风格引导"
    new_body = "过程改写成完整的一节。" * 42
    replaced = s10.replace_section(draft, doc_id, "过程", new_body)
    replaced += "\n## 结果\n结果还没有写完。\n"
    assert s10.rewrite_ok(base.first_body(draft, "过程", doc_id), new_body, doc_id, "过程")["ok"]
    verdict = s10.judge_doc(draft, replaced, doc_id, False)
    assert verdict["true_complete"] is False
    assert s8.needs_repair(replaced, doc_id, "结果")
    tries = {"结果": 3}
    assert not [title for title in ("结果",) if s8.needs_repair(replaced, doc_id, title) and tries[title] < MAX_TRIES]
    assert base.estimate("甲" * 2000) == 2000


def append_scaffold(text: str) -> dict[str, object]:
    hits = scaffold_hits(text)
    s10._patch_last({"scaffold_hits": hits})
    return {"scaffold_hits": hits, "scaffold_rejected": bool(hits)}


def reject_scaffold(run_id: str, doc_id: str, repeat: int, messages: list[dict[str, str]], cap: int, round_index: int, result: dict[str, object]) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
    extra = [result]
    info = append_scaffold(str(result["text"]))
    if not info["scaffold_rejected"]:
        return result, extra
    second = s10.call(run_id, doc_id, repeat, "scaffold_retry", round_index, scaffold_messages(messages), cap, 2, str(result.get("phase", "")))
    extra.append(second)
    info = append_scaffold(str(second["text"]))
    if info["scaffold_rejected"] or not second["accepted"]:
        return None, extra
    return second, extra


def repair_once(run_id: str, doc_id: str, repeat: int, phase: str, round_index: int, messages: list[dict[str, str]], cap: int, existing: str) -> tuple[list[dict[str, object]], str | None, dict[str, object]]:
    calls: list[dict[str, object]] = []
    first = s10.call(run_id, doc_id, repeat, phase, round_index, messages, cap, 1)
    calls.append(first)
    if not first["accepted"]:
        return calls, None, {"novelty_rejected": False, "guard_hits": first["guard_hits"], "scaffold_hits": []}
    kept, extra = reject_scaffold(run_id, doc_id, repeat, messages, cap, round_index, first)
    if len(extra) > 1:
        calls.append(extra[-1])
    if kept is None:
        return calls, None, {"scaffold_rejected": True, "scaffold_hits": scaffold_hits(str(calls[-1]["text"])), "novelty_rejected": False}
    check = s10.novelty_check(existing, str(kept["text"]))
    s10._patch_last(check)
    if not check["novelty_rejected"]:
        return calls, str(kept["text"]), check
    retried = [dict(item) for item in messages]
    retried[-1]["content"] = retried[-1]["content"] + "\n\n" + s10.NOVELTY_RETRY
    second = s10.call(run_id, doc_id, repeat, "novelty_retry", round_index, retried, cap, 2, phase)
    calls.append(second)
    if not second["accepted"]:
        return calls, None, {"novelty_rejected": False, "guard_hits": second["guard_hits"], "scaffold_hits": []}
    kept, extra = reject_scaffold(run_id, doc_id, repeat, retried, cap, round_index, second)
    if len(extra) > 1:
        calls.append(extra[-1])
    if kept is None:
        return calls, None, {"scaffold_rejected": True, "scaffold_hits": scaffold_hits(str(calls[-1]["text"])), "novelty_rejected": False}
    check = s10.novelty_check(existing, str(kept["text"]))
    s10._patch_last(check)
    if check["novelty_rejected"]:
        return calls, None, check
    return calls, str(kept["text"]), check


def run_f(doc_id: str, repeat: int, draft: str) -> None:
    run_id = f"{doc_id}-F-{repeat}"
    calls: list[dict[str, object]] = []
    repairs: list[dict[str, object]] = []
    rewrites: list[dict[str, object]] = []
    text = draft
    failed = False
    fail_shape = ""
    current_title = base.present_headings(draft, doc_id)[-1]
    anchor_lost = 0
    unresolved_anchor = False
    finish_rounds = 0
    tries = {title: 0 for title in base.headings_of(doc_id)}
    for _attempt in range(s8.MAX_FINISH_ROUNDS):
        if failed or base.open_section(text, doc_id)[0] is None:
            break
        attempts, result, anchor = s10.guarded_finish(
            run_id, doc_id, repeat, "finish_current", finish_rounds + 1,
            base.finish_messages(text, doc_id), s8.FINISH_CAP, s9.current_tail(text, doc_id, current_title),
        )
        calls.extend(attempts)
        finish_rounds += 1
        if anchor and anchor["anchor_lost"]:
            anchor_lost += 1
        if result is not None:
            kept, extra = reject_scaffold(run_id, doc_id, repeat, base.finish_messages(text, doc_id), s8.FINISH_CAP, finish_rounds, result)
            if len(extra) > 1:
                calls.append(extra[-1])
            if kept is None:
                failed = True
                fail_shape = "scaffold"
                break
            text = text.rstrip() + "\n\n" + str(kept["text"]).strip() + "\n"
            if base.sentence_closed(base.first_body(text, current_title, doc_id)):
                break
            continue
        leaked = any(item.get("guard_hits") for item in attempts)
        lost = bool(anchor and anchor["anchor_lost"])
        if not (leaked or lost):
            failed = True
            fail_shape = "finish_empty"
            break
        old = base.last_bodies(text, doc_id).get(current_title, "")
        rewritten = s10.call(run_id, doc_id, repeat, "rewrite_current", 1, s10.rewrite_messages(text, doc_id, current_title), s10.REWRITE_CAP, 1)
        calls.append(rewritten)
        kept, extra = reject_scaffold(run_id, doc_id, repeat, s10.rewrite_messages(text, doc_id, current_title), s10.REWRITE_CAP, 1, rewritten)
        if len(extra) > 1:
            calls.append(extra[-1])
        tries[current_title] += 1
        if kept is None:
            failed = True
            fail_shape = "scaffold"
            unresolved_anchor = lost
            break
        report = s10.rewrite_ok(old, str(kept["text"]), doc_id, current_title)
        s10._patch_last(report)
        rewrites.append(report)
        if report["ok"]:
            text = s10.replace_section(text, doc_id, current_title, str(kept["text"]))
        else:
            failed = True
            fail_shape = "rewrite_rejected"
            unresolved_anchor = lost
        break
    while not failed and s8.needs_repair(text, doc_id, current_title) and tries[current_title] < MAX_TRIES:
        before = s10.section_len(text, doc_id, current_title)
        existing = base.last_bodies(text, doc_id).get(current_title, "")
        tries[current_title] += 1
        generated, addition, check = repair_once(
            run_id, doc_id, repeat, "repair_current", tries[current_title],
            s9.repair_messages(text, doc_id, current_title, True), s8.repair_cap(text, doc_id, current_title), existing,
        )
        calls.extend(generated)
        if addition is None:
            repairs.append({"phase": "repair_current", "title": current_title, "attempt": tries[current_title], "before_chars": before, "after_chars": before, "accepted": False, **check})
            failed = True
            fail_shape = "scaffold" if check.get("scaffold_rejected") else ("guard_rejected" if check.get("guard_hits") else "novelty_rejected")
            break
        text = s8.splice_section(text, doc_id, current_title, addition)
        repairs.append({"phase": "repair_current", "title": current_title, "attempt": tries[current_title], "before_chars": before, "after_chars": s10.section_len(text, doc_id, current_title), "accepted": True, **check})
    pending_calls = 0
    if not failed:
        missing = [title for title in base.headings_of(doc_id) if tries[title] == 0 and s8.needs_repair(text, doc_id, title)]
        if missing:
            messages = base.pending_batch_messages(text, doc_id, missing)
            result = s10.call(run_id, doc_id, repeat, "write_pending", 1, messages, base.pending_output_cap(len(missing)), 1)
            calls.append(result)
            pending_calls = 1
            if not result["accepted"]:
                failed = True
                fail_shape = "guard_rejected"
            else:
                kept, extra = reject_scaffold(run_id, doc_id, repeat, messages, base.pending_output_cap(len(missing)), 1, result)
                if len(extra) > 1:
                    calls.append(extra[-1])
                if kept is None:
                    failed = True
                    fail_shape = "scaffold"
                else:
                    text = text.rstrip() + "\n\n" + str(kept["text"]).strip() + "\n"
    while not failed:
        shorts = [title for title in base.headings_of(doc_id) if s8.needs_repair(text, doc_id, title) and tries[title] < MAX_TRIES]
        if not shorts:
            break
        title = shorts[0]
        before = s10.section_len(text, doc_id, title)
        existing = base.last_bodies(text, doc_id).get(title, "")
        tries[title] += 1
        phase = "repair_current" if title == current_title else "repair_pending"
        generated, addition, check = repair_once(
            run_id, doc_id, repeat, phase, tries[title],
            s9.repair_messages(text, doc_id, title, title == current_title), s8.repair_cap(text, doc_id, title), existing,
        )
        calls.extend(generated)
        if addition is None:
            repairs.append({"phase": phase, "title": title, "attempt": tries[title], "before_chars": before, "after_chars": before, "accepted": False, **check})
            failed = True
            fail_shape = "scaffold" if check.get("scaffold_rejected") else ("guard_rejected" if check.get("guard_hits") else "novelty_rejected")
            break
        text = s8.splice_section(text, doc_id, title, addition)
        repairs.append({"phase": phase, "title": title, "attempt": tries[title], "before_chars": before, "after_chars": s10.section_len(text, doc_id, title), "accepted": True, **check})
    verdict = s10.judge_doc(draft, text, doc_id, failed and fail_shape == "guard_rejected")
    hits = scaffold_hits(text)
    if hits:
        verdict["failure_shapes"] = list(verdict["failure_shapes"]) + ["scaffold"]
        verdict["true_complete"] = False
        verdict["scaffold_hits"] = hits
    if unresolved_anchor:
        verdict["failure_shapes"] = list(verdict["failure_shapes"]) + ["anchor_lost"]
        verdict["true_complete"] = False
    if failed and fail_shape and fail_shape not in verdict["failure_shapes"]:
        verdict["failure_shapes"] = list(verdict["failure_shapes"]) + [fail_shape]
        verdict["true_complete"] = False
    finalized = 0
    if verdict["true_complete"] and not verdict["has_ending"] and not failed and not hits:
        result = s10.call(run_id, doc_id, repeat, "finalize", 1, base.finalize_messages(), s8.FINAL_CAP, 1)
        calls.append(result)
        finalized = 1
        if result["accepted"] and str(result["text"]).strip() and not scaffold_hits(str(result["text"])):
            text = text.rstrip() + "\n\n" + str(result["text"]).strip() + "\n"
            verdict = s10.judge_doc(draft, text, doc_id, False)
        else:
            verdict = s10.judge_doc(draft, text, doc_id, bool(result["guard_hits"]) or bool(scaffold_hits(str(result["text"]))))
            if scaffold_hits(str(result["text"])):
                verdict["failure_shapes"] = list(verdict["failure_shapes"]) + ["scaffold"]
                verdict["true_complete"] = False
    s10.store_run(doc_id, repeat, draft, text, calls, repairs, rewrites, verdict, anchor_lost, {
        "finish_current": finish_rounds,
        "repair_current": sum(1 for item in repairs if item["phase"] == "repair_current"),
        "write_pending": pending_calls,
        "repair_pending": sum(1 for item in repairs if item["phase"] == "repair_pending"),
        "rewrite_current": len(rewrites),
        "finalize": finalized,
        "tries": tries,
        "unresolved_anchor": unresolved_anchor,
    })


def summarize() -> None:
    s10.summarize()
    rows = [json.loads(line) for line in RUNS.read_text(encoding="utf-8").splitlines() if line.strip()]
    requests = [json.loads(line) for line in REQUESTS.read_text(encoding="utf-8").splitlines() if line.strip()]
    fresh = [item for item in requests if item.get("phase") != "draft"]
    report = json.loads(SUMMARY.read_text(encoding="utf-8"))
    report["scaffold_hits"] = sum(1 for item in fresh if item.get("scaffold_hits"))
    report["scaffold_retries"] = sum(1 for item in fresh if item.get("phase") == "scaffold_retry")
    report["unresolved_anchor"] = sum(1 for row in rows if row.get("phases", {}).get("unresolved_anchor"))
    SUMMARY.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"scaffold_hits": report["scaffold_hits"], "scaffold_retries": report["scaffold_retries"], "unresolved_anchor": report["unresolved_anchor"]}, ensure_ascii=False), flush=True)


def main() -> None:
    self_check()
    finished = s10.done_ids()
    drafts = s10.load_drafts()
    for text in drafts.values():
        if scaffold_hits(text):
            raise SystemExit(f"初稿里有脚手架标签：{scaffold_hits(text)}")
    for doc_id in base.DOCS:
        for repeat in range(1, s10.REPEATS + 1):
            if f"{doc_id}-F-{repeat}" in finished:
                continue
            run_f(doc_id, repeat, drafts[doc_id])
    summarize()


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPError as exc:
        raise SystemExit(f"llama-server 不可用：{exc}") from exc
