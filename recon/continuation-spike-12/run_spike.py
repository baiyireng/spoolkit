"""L0.14：一节失败不停下其他节。每节最多补两次，仍不达标就整节改写替换。

当前节和未开始的节走同一套。改写成功不代替全文判定。不进产品路径。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
PRIOR_PATH = ROOT.parent / "continuation-spike-11" / "run_spike.py"
REQUESTS = ROOT / "requests.jsonl"
RUNS = ROOT / "runs.jsonl"
SUMMARY = ROOT / "summary.json"
MAX_REPAIR = 2


def load_prior():
    spec = importlib.util.spec_from_file_location("spike11", PRIOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("找不到 L0.13 脚本")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s11 = load_prior()
s10 = s11.s10
s10.REQUESTS = REQUESTS
s10.RUNS = RUNS
s10.SUMMARY = SUMMARY
base = s10.base
s8 = s10.s8
s9 = s10.s9


def next_step(repairs: int, last_failed: bool, still_short: bool, rewritten: bool) -> str:
    if rewritten:
        return "stop"
    if not still_short and not last_failed:
        return "done"
    if repairs < MAX_REPAIR and not last_failed:
        return "repair"
    return "rewrite"


def self_check() -> None:
    assert next_step(0, False, True, False) == "repair"
    assert next_step(1, False, True, False) == "repair"
    assert next_step(1, True, True, False) == "rewrite"
    assert next_step(2, False, True, False) == "rewrite"
    assert next_step(1, False, False, False) == "done"
    assert next_step(2, True, True, True) == "stop"
    visited: list[str] = []
    for title, failed in (("改法", True), ("下一步", False)):
        visited.append(title)
        if failed:
            assert next_step(2, True, True, False) == "rewrite"
        else:
            assert next_step(0, False, True, False) == "repair"
    assert visited == ["改法", "下一步"]
    doc_id = "retro"
    draft = "## 目标\n" + ("目标已经写完。" * 70) + "\n\n## 过程\n过程写到一半，风格引导"
    new_body = "过程改写成完整的一节。" * 42
    replaced = s10.replace_section(draft, doc_id, "过程", new_body)
    assert "风格引导" not in base.last_bodies(replaced, doc_id)["过程"]
    assert "目标已经写完。" in replaced
    assert base.estimate("甲" * 2000) == 2000


def reason_of(check: dict[str, object]) -> str:
    if check.get("scaffold_rejected") or check.get("scaffold_hits"):
        return "scaffold"
    if check.get("guard_hits"):
        return "guard"
    if check.get("novelty_rejected"):
        return "novelty"
    return "short"


def run_f(doc_id: str, repeat: int, draft: str) -> None:
    run_id = f"{doc_id}-F-{repeat}"
    calls: list[dict[str, object]] = []
    repairs: list[dict[str, object]] = []
    rewrites: list[dict[str, object]] = []
    text = draft
    current_title = base.present_headings(draft, doc_id)[-1]
    anchor_lost = 0
    unresolved_anchor = False
    finish_rounds = 0
    stats = {title: {"title": title, "repairs": 0, "rewrites": 0, "reason": "ok"} for title in base.headings_of(doc_id)}
    handled: set[str] = set()

    def rewrite_section(title: str, prior: str) -> None:
        nonlocal text
        old = base.last_bodies(text, doc_id).get(title, "")
        messages = s10.rewrite_messages(text, doc_id, title)
        result = s10.call(run_id, doc_id, repeat, "rewrite_section", stats[title]["rewrites"] + 1, messages, s10.REWRITE_CAP, 1)
        calls.append(result)
        if not result["accepted"]:
            retry_messages = s9.prev.retry_messages(messages)
            result = s10.call(run_id, doc_id, repeat, "guard_retry", stats[title]["rewrites"] + 1, retry_messages, s10.REWRITE_CAP, 2, "rewrite_section")
            calls.append(result)
            messages = retry_messages
        kept, extra = s11.reject_scaffold(run_id, doc_id, repeat, messages, s10.REWRITE_CAP, stats[title]["rewrites"] + 1, result)
        if len(extra) > 1:
            calls.append(extra[-1])
        stats[title]["rewrites"] += 1
        if kept is None or not kept["accepted"]:
            stats[title]["reason"] = "scaffold" if kept is None else "guard"
            rewrites.append({"title": title, "ok": False, "reason": stats[title]["reason"], "prior": prior})
            return
        report = s10.rewrite_ok(old, str(kept["text"]), doc_id, title)
        s10._patch_last({"title": title, **report})
        if report["ok"]:
            text = s10.replace_section(text, doc_id, title, str(kept["text"]))
            stats[title]["reason"] = "ok"
        else:
            why = "scaffold" if report["guard_hits"] else "rewrite_rejected"
            stats[title]["reason"] = why
            report = dict(report)
            report["reason"] = why
        rewrites.append({"title": title, "prior": prior, **report})

    def recover(title: str) -> None:
        nonlocal text
        if title in handled:
            return
        handled.add(title)
        repairs_done = 0
        last_failed = False
        prior = "short"
        while True:
            still = s8.needs_repair(text, doc_id, title)
            rewritten = stats[title]["rewrites"] > 0
            step = next_step(repairs_done, last_failed, still, rewritten)
            if step in {"done", "stop"}:
                if still and stats[title]["reason"] == "ok":
                    stats[title]["reason"] = prior
                break
            if step == "repair":
                before = s10.section_len(text, doc_id, title)
                existing = base.last_bodies(text, doc_id).get(title, "")
                repairs_done += 1
                phase = "repair_current" if title == current_title else "repair_pending"
                generated, addition, check = s11.repair_once(
                    run_id, doc_id, repeat, phase, repairs_done,
                    s9.repair_messages(text, doc_id, title, title == current_title),
                    s8.repair_cap(text, doc_id, title), existing,
                )
                calls.extend(generated)
                stats[title]["repairs"] = repairs_done
                if addition is None:
                    last_failed = True
                    prior = reason_of(check)
                    repairs.append({"phase": phase, "title": title, "attempt": repairs_done, "before_chars": before, "after_chars": before, "accepted": False, **check})
                    continue
                text = s8.splice_section(text, doc_id, title, addition)
                last_failed = False
                prior = "short"
                repairs.append({"phase": phase, "title": title, "attempt": repairs_done, "before_chars": before, "after_chars": s10.section_len(text, doc_id, title), "accepted": True, **check})
                continue
            rewrite_section(title, prior)
            break

    for _attempt in range(s8.MAX_FINISH_ROUNDS):
        if base.open_section(text, doc_id)[0] is None:
            break
        attempts, result, anchor = s10.guarded_finish(
            run_id, doc_id, repeat, "finish_current", finish_rounds + 1,
            base.finish_messages(text, doc_id), s8.FINISH_CAP, s9.current_tail(text, doc_id, current_title),
        )
        calls.extend(attempts)
        finish_rounds += 1
        if anchor and anchor["anchor_lost"]:
            anchor_lost += 1
        if result is None:
            lost = bool(anchor and anchor["anchor_lost"])
            leaked = any(item.get("guard_hits") for item in attempts)
            if leaked or lost:
                rewrite_section(current_title, "anchor_lost" if lost else "guard")
                if lost and stats[current_title]["reason"] != "ok":
                    unresolved_anchor = True
                handled.add(current_title)
            break
        kept, extra = s11.reject_scaffold(run_id, doc_id, repeat, base.finish_messages(text, doc_id), s8.FINISH_CAP, finish_rounds, result)
        if len(extra) > 1:
            calls.append(extra[-1])
        if kept is None:
            stats[current_title]["reason"] = "scaffold"
            rewrite_section(current_title, "scaffold")
            handled.add(current_title)
            break
        text = text.rstrip() + "\n\n" + str(kept["text"]).strip() + "\n"
        if base.sentence_closed(base.first_body(text, current_title, doc_id)):
            break
    if current_title not in handled and s8.needs_repair(text, doc_id, current_title):
        recover(current_title)
    else:
        handled.add(current_title)
    pending_calls = 0
    missing = [title for title in base.headings_of(doc_id) if title not in handled and s8.needs_repair(text, doc_id, title)]
    if missing:
        messages = base.pending_batch_messages(text, doc_id, missing)
        result = s10.call(run_id, doc_id, repeat, "write_pending", 1, messages, base.pending_output_cap(len(missing)), 1)
        calls.append(result)
        pending_calls = 1
        if result["accepted"]:
            kept, extra = s11.reject_scaffold(run_id, doc_id, repeat, messages, base.pending_output_cap(len(missing)), 1, result)
            if len(extra) > 1:
                calls.append(extra[-1])
            if kept is not None:
                text = text.rstrip() + "\n\n" + str(kept["text"]).strip() + "\n"
    for title in base.headings_of(doc_id):
        if s8.needs_repair(text, doc_id, title):
            recover(title)
    for title in stats:
        stats[title]["chars"] = s10.section_len(text, doc_id, title)
    verdict = s10.judge_doc(draft, text, doc_id, False)
    hits = s11.scaffold_hits(text)
    if hits:
        verdict["failure_shapes"] = list(verdict["failure_shapes"]) + ["scaffold"]
        verdict["true_complete"] = False
    if unresolved_anchor:
        verdict["failure_shapes"] = list(verdict["failure_shapes"]) + ["anchor_lost"]
        verdict["true_complete"] = False
    finalized = 0
    if verdict["true_complete"] and not verdict["has_ending"] and not hits:
        result = s10.call(run_id, doc_id, repeat, "finalize", 1, base.finalize_messages(), s8.FINAL_CAP, 1)
        calls.append(result)
        finalized = 1
        if result["accepted"] and str(result["text"]).strip() and not s11.scaffold_hits(str(result["text"])):
            text = text.rstrip() + "\n\n" + str(result["text"]).strip() + "\n"
            verdict = s10.judge_doc(draft, text, doc_id, False)
        else:
            blocked = bool(result["guard_hits"]) or bool(s11.scaffold_hits(str(result["text"])))
            verdict = s10.judge_doc(draft, text, doc_id, blocked)
    s10.store_run(doc_id, repeat, draft, text, calls, repairs, rewrites, verdict, anchor_lost, {
        "finish_current": finish_rounds,
        "repair_current": sum(1 for item in repairs if item["phase"] == "repair_current"),
        "write_pending": pending_calls,
        "repair_pending": sum(1 for item in repairs if item["phase"] == "repair_pending"),
        "rewrite_section": sum(item["rewrites"] for item in stats.values()),
        "finalize": finalized,
        "unresolved_anchor": unresolved_anchor,
        "section_log": list(stats.values()),
    })


def summarize() -> None:
    s10.summarize()
    rows = [json.loads(line) for line in RUNS.read_text(encoding="utf-8").splitlines() if line.strip()]
    requests = [json.loads(line) for line in REQUESTS.read_text(encoding="utf-8").splitlines() if line.strip()]
    fresh = [item for item in requests if item.get("phase") != "draft"]
    report = json.loads(SUMMARY.read_text(encoding="utf-8"))
    report["scaffold_hits"] = sum(1 for item in fresh if item.get("scaffold_hits"))
    report["rewrites"] = sum(1 for item in fresh if item.get("phase") == "rewrite_section")
    report["unresolved_anchor"] = sum(1 for row in rows if row.get("phases", {}).get("unresolved_anchor"))
    SUMMARY.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"scaffold_hits": report["scaffold_hits"], "rewrites": report["rewrites"], "unresolved_anchor": report["unresolved_anchor"]}, ensure_ascii=False), flush=True)


def main() -> None:
    self_check()
    finished = s10.done_ids()
    drafts = s10.load_drafts()
    for text in drafts.values():
        if s11.scaffold_hits(text):
            raise SystemExit(f"初稿里有脚手架标签：{s11.scaffold_hits(text)}")
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
