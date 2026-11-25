"""L0.11：当前节不足 450 字也补，并检查重试是否接上尾部。不进产品路径。

finish_current 之后，当前节不足 450 字就先 repair_current，最多两次，再 write_pending。
未开始节仍按不足再 repair_pending。
守卫拒绝的返回不进正文。重试开头若看不到当前节最后 12 字，也看不到被拒绝返回的最后 12 字，记 anchor_lost。
anchor_lost 不算真完成。连续两次就停止，不再往下写。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
PRIOR_PATH = ROOT.parent / "continuation-spike-8" / "run_spike.py"
REQUESTS = ROOT / "requests.jsonl"
RUNS = ROOT / "runs.jsonl"
SUMMARY = ROOT / "summary.json"
DRAFT_SOURCE = ROOT.parent / "continuation-spike-8" / "requests.jsonl"
ANCHOR_CHARS = 12
ANCHOR_HEAD = 160
MAX_REPAIRS = 2
REPEATS = 3


def load_prior():
    spec = importlib.util.spec_from_file_location("spike8", PRIOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("找不到 L0.10 脚本")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prev = load_prior()
base = prev.base
SECTION_FLOOR = prev.SECTION_FLOOR
CEILING = prev.CEILING


def repair_messages(text: str, doc_id: str, title: str, current: bool) -> list[dict[str, str]]:
    messages = prev.repair_messages(text, doc_id, title)
    if not current:
        return messages
    rewritten: list[dict[str, str]] = []
    for item in messages:
        content = item["content"].replace("只补完这一节", "只补完当前节")
        rewritten.append({"role": item["role"], "content": content})
    return rewritten


def anchor_check(current_tail: str, rejected_text: str, retry_text: str) -> dict[str, object]:
    head = retry_text.strip()[:ANCHOR_HEAD]
    current_anchor = current_tail.strip()[-ANCHOR_CHARS:]
    rejected_anchor = rejected_text.strip()[-ANCHOR_CHARS:]
    current_hit = bool(current_anchor) and current_anchor in head
    rejected_hit = bool(rejected_anchor) and rejected_anchor in head
    gram_current = base.overlap_ratio(current_tail.strip()[-40:], head[:80]) if current_tail.strip() else 0.0
    gram_rejected = base.overlap_ratio(rejected_text.strip()[-40:], head[:80]) if rejected_text.strip() else 0.0
    return {
        "anchor_lost": not current_hit and not rejected_hit,
        "current_anchor": current_anchor,
        "rejected_anchor": rejected_anchor,
        "current_hit": current_hit,
        "rejected_hit": rejected_hit,
        "gram_overlap_current": round(gram_current, 4),
        "gram_overlap_rejected": round(gram_rejected, 4),
    }


def self_check() -> None:
    current = "足够的自主生成能力，能够根据结构自行填充内容；大纲+示例模式假设模型需要风格引导"
    rejected = "断或格式错误导致无法参与评分的样本，并在日志中记录剔除比例，以便后续敏感性分析。"
    restarted = (
        "范模型的输出。这三种输入模式分别对应了不同的实验假设："
        "纯大纲模式假设模型具备足够的自主生成能力，能够仅凭结构约束自行填充内容；大纲加示例模式假设模型需要风格与格"
    )
    lost = anchor_check(current, rejected, restarted)
    assert lost["anchor_lost"]
    assert lost["current_hit"] is False and lost["rejected_hit"] is False
    assert float(lost["gram_overlap_current"]) > 0
    kept = anchor_check(current, rejected, current[-ANCHOR_CHARS:] + "。后面把这一节写完。")
    assert kept["anchor_lost"] is False and kept["current_hit"] is True
    doc_id = "tech"
    draft = "## 窗口\n" + ("窗口已经写完。" * 70) + "\n\n## 余量\n" + ("余量已经写了一些。" * 20)
    assert prev.needs_repair(draft, doc_id, "余量")
    grown = prev.splice_section(draft, doc_id, "余量", "余量补到目标长度。" * 40)
    assert len(base.last_bodies(grown, doc_id)["余量"]) >= SECTION_FLOOR
    assert not prev.needs_repair(grown, doc_id, "余量")
    messages = repair_messages(draft, doc_id, "余量", True)
    assert "只补完当前节" in messages[-1]["content"]
    assert "80" not in messages[1]["content"]
    verdict = prev.judge(draft, grown, doc_id, False)
    verdict["true_complete"] = False
    verdict["failure_shapes"] = list(verdict["failure_shapes"]) + ["anchor_lost"]
    assert "anchor_lost" in verdict["failure_shapes"]
    assert base.estimate("甲" * 2000) == 2000


def append(path: Path, row: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        handle.flush()


def done_ids() -> set[str]:
    if not RUNS.exists():
        return set()
    return {
        str(json.loads(line)["run_id"])
        for line in RUNS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def call(
    *,
    run_id: str,
    doc_id: str,
    repeat: int,
    phase: str,
    round_index: int,
    messages: list[dict[str, str]],
    cap: int,
    attempt: int,
    retried_phase: str = "",
    anchor: dict[str, object] | None = None,
) -> dict[str, object]:
    max_tokens, estimate_prompt = base.output_budget(messages, cap)
    print(
        f"{run_id} {phase} a{attempt} r{round_index} estimate={estimate_prompt} max_tokens={max_tokens}",
        flush=True,
    )
    if max_tokens <= 0:
        result: dict[str, object] = {
            "text": "",
            "finish_reason": "budget",
            "truncated": True,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "ttft_s": 0.0,
            "elapsed_s": 0.0,
        }
    else:
        result = base.post(messages, max_tokens)
    actual = int(result["prompt_tokens"])
    guard = base.output_guard(str(result["text"]))
    accepted = not guard["guard_hits"] and bool(str(result["text"]).strip())
    within = (actual + max_tokens) <= CEILING and (estimate_prompt + max_tokens) <= CEILING
    anchor = anchor or {}
    result.update(
        {
            "phase": phase,
            "max_tokens": max_tokens,
            "within_budget": within,
            "accepted": accepted,
            "guard_hits": guard["guard_hits"],
            "anchor_lost": bool(anchor.get("anchor_lost", False)),
        }
    )
    append(
        REQUESTS,
        {
            "run_id": run_id,
            "strategy": "F",
            "doc_id": doc_id,
            "repeat": repeat,
            "phase": phase,
            "attempt": attempt,
            "round": round_index,
            "max_tokens": max_tokens,
            "estimate_prompt_tokens": estimate_prompt,
            "prompt_tokens": actual,
            "completion_tokens": result["completion_tokens"],
            "truncated": result["truncated"],
            "finish_reason": result["finish_reason"],
            "ttft_s": result["ttft_s"],
            "elapsed_s": result["elapsed_s"],
            "estimate_minus_actual": estimate_prompt - actual,
            "within_budget": within,
            "prompt_plus_max": actual + max_tokens,
            "ceiling": CEILING,
            "accepted": accepted,
            "guard_hits": guard["guard_hits"],
            "meta_terms": guard["meta_terms"],
            "english_chars": guard["english_chars"],
            "retried_phase": retried_phase,
            "anchor_lost": bool(anchor.get("anchor_lost", False)),
            "current_hit": anchor.get("current_hit"),
            "rejected_hit": anchor.get("rejected_hit"),
            "gram_overlap_current": anchor.get("gram_overlap_current"),
            "gram_overlap_rejected": anchor.get("gram_overlap_rejected"),
            "messages": messages,
            "text": result["text"],
        },
    )
    print(
        f"  tokens={actual}+{result['completion_tokens']} truncated={result['truncated']} "
        f"accepted={accepted} guard={guard['guard_hits'] or '-'} "
        f"anchor={anchor.get('anchor_lost', '-')} {result['elapsed_s']}s",
        flush=True,
    )
    return result


def guarded_call(
    *,
    run_id: str,
    doc_id: str,
    repeat: int,
    phase: str,
    round_index: int,
    messages: list[dict[str, str]],
    cap: int,
    current_tail: str,
) -> tuple[list[dict[str, object]], dict[str, object] | None, dict[str, object] | None]:
    first = call(
        run_id=run_id,
        doc_id=doc_id,
        repeat=repeat,
        phase=phase,
        round_index=round_index,
        messages=messages,
        cap=cap,
        attempt=1,
    )
    attempts = [first]
    if first["accepted"]:
        return attempts, first, None
    if not first["guard_hits"]:
        return attempts, None, None
    anchor = anchor_check(current_tail, str(first["text"]), "")
    second_messages = prev.retry_messages(messages)
    # The retry text is unknown until the call returns. Score it after, then the row already
    # lacks the score. Call first with an empty anchor and patch the saved row below.
    second = call(
        run_id=run_id,
        doc_id=doc_id,
        repeat=repeat,
        phase="guard_retry",
        round_index=round_index,
        messages=second_messages,
        cap=cap,
        attempt=2,
        retried_phase=phase,
    )
    anchor = anchor_check(current_tail, str(first["text"]), str(second["text"]))
    second["anchor_lost"] = anchor["anchor_lost"]
    second["anchor"] = anchor
    _patch_last_anchor(anchor)
    attempts.append(second)
    return attempts, second if second["accepted"] else None, anchor


def _patch_last_anchor(anchor: dict[str, object]) -> None:
    lines = REQUESTS.read_text(encoding="utf-8").splitlines()
    if not lines:
        return
    row = json.loads(lines[-1])
    row.update(
        {
            "anchor_lost": anchor["anchor_lost"],
            "current_hit": anchor["current_hit"],
            "rejected_hit": anchor["rejected_hit"],
            "gram_overlap_current": anchor["gram_overlap_current"],
            "gram_overlap_rejected": anchor["gram_overlap_rejected"],
            "current_anchor": anchor["current_anchor"],
            "rejected_anchor": anchor["rejected_anchor"],
        }
    )
    lines[-1] = json.dumps(row, ensure_ascii=False)
    REQUESTS.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_drafts() -> dict[str, str]:
    if REQUESTS.exists():
        found: dict[str, str] = {}
        for line in REQUESTS.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if item.get("phase") == "draft" and item.get("draft_ok"):
                found[str(item["doc_id"])] = str(item["text"])
        if len(found) == len(base.DOCS):
            return found
    prior = [json.loads(line) for line in DRAFT_SOURCE.read_text(encoding="utf-8").splitlines() if line.strip()]
    loaded: dict[str, str] = {}
    for doc_id in base.DOCS:
        source = next(
            item
            for item in prior
            if item.get("phase") == "draft" and item.get("draft_ok") and item["doc_id"] == doc_id
        )
        text = str(source["text"]).rstrip()
        current = base.present_headings(text, doc_id)[-1]
        if base.sentence_closed(base.first_body(text, current, doc_id)):
            raise SystemExit(f"{doc_id} 当前节不是半句")
        print(f"{doc_id} current={current} chars={len(text)} end={text[-24:]!r}", flush=True)
        append(
            REQUESTS,
            {
                "run_id": f"{doc_id}-draft",
                "strategy": "draft",
                "doc_id": doc_id,
                "repeat": 1,
                "phase": "draft",
                "round": 0,
                "draft_ok": True,
                "reused_from": "continuation-spike-8",
                "text": text,
                "within_budget": True,
                "accepted": True,
                "elapsed_s": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
            },
        )
        loaded[doc_id] = text
    return loaded


def section_len(text: str, doc_id: str, title: str) -> int:
    return len(base.last_bodies(text, doc_id).get(title, "").strip())


def current_tail(text: str, doc_id: str, title: str) -> str:
    return base.last_bodies(text, doc_id).get(title, "")


class AnchorStreak:
    def __init__(self) -> None:
        self.count = 0
        self.stopped = False
        self.events: list[dict[str, object]] = []

    def observe(self, phase: str, anchor: dict[str, object] | None) -> None:
        if anchor is None:
            self.count = 0
            return
        event = {"phase": phase, **anchor}
        self.events.append(event)
        if anchor["anchor_lost"]:
            self.count += 1
            if self.count >= 2:
                self.stopped = True
        else:
            self.count = 0


def run_f(doc_id: str, repeat: int, draft: str) -> None:
    run_id = f"{doc_id}-F-{repeat}"
    calls: list[dict[str, object]] = []
    repairs: list[dict[str, object]] = []
    text = draft
    guard_failed = False
    streak = AnchorStreak()
    current_title = base.present_headings(draft, doc_id)[-1]
    finish_rounds = 0
    for _attempt in range(prev.MAX_FINISH_ROUNDS):
        if streak.stopped or guard_failed or base.open_section(text, doc_id)[0] is None:
            break
        attempts, result, anchor = guarded_call(
            run_id=run_id,
            doc_id=doc_id,
            repeat=repeat,
            phase="finish_current",
            round_index=finish_rounds + 1,
            messages=base.finish_messages(text, doc_id),
            cap=prev.FINISH_CAP,
            current_tail=current_tail(text, doc_id, current_title),
        )
        calls.extend(attempts)
        finish_rounds += 1
        streak.observe("finish_current", anchor)
        if result is None:
            if attempts[-1]["guard_hits"]:
                guard_failed = True
            break
        text = text.rstrip() + "\n\n" + str(result["text"]).strip() + "\n"
        if base.sentence_closed(base.first_body(text, current_title, doc_id)):
            break
    current_repairs = 0
    while (
        not streak.stopped
        and not guard_failed
        and prev.needs_repair(text, doc_id, current_title)
        and current_repairs < MAX_REPAIRS
    ):
        before = section_len(text, doc_id, current_title)
        attempts, result, anchor = guarded_call(
            run_id=run_id,
            doc_id=doc_id,
            repeat=repeat,
            phase="repair_current",
            round_index=current_repairs + 1,
            messages=repair_messages(text, doc_id, current_title, True),
            cap=prev.repair_cap(text, doc_id, current_title),
            current_tail=current_tail(text, doc_id, current_title),
        )
        calls.extend(attempts)
        current_repairs += 1
        streak.observe("repair_current", anchor)
        if result is None:
            repairs.append(
                {
                    "phase": "repair_current",
                    "title": current_title,
                    "attempt": current_repairs,
                    "before_chars": before,
                    "after_chars": before,
                    "accepted": False,
                }
            )
            if attempts[-1]["guard_hits"]:
                guard_failed = True
            break
        text = prev.splice_section(text, doc_id, current_title, str(result["text"]))
        repairs.append(
            {
                "phase": "repair_current",
                "title": current_title,
                "attempt": current_repairs,
                "before_chars": before,
                "after_chars": section_len(text, doc_id, current_title),
                "accepted": True,
                "anchor_lost": bool(anchor and anchor["anchor_lost"]),
            }
        )
    pending_calls = 0
    original_pending = base.unfinished(draft, doc_id)
    if not streak.stopped and not guard_failed:
        missing = [title for title in original_pending if prev.needs_repair(text, doc_id, title)]
        if missing:
            attempts, result, anchor = guarded_call(
                run_id=run_id,
                doc_id=doc_id,
                repeat=repeat,
                phase="write_pending",
                round_index=1,
                messages=base.pending_batch_messages(text, doc_id, missing),
                cap=base.pending_output_cap(len(missing)),
                current_tail=current_tail(text, doc_id, current_title),
            )
            calls.extend(attempts)
            pending_calls = 1
            streak.observe("write_pending", anchor)
            if result is None:
                if attempts[-1]["guard_hits"]:
                    guard_failed = True
            else:
                text = text.rstrip() + "\n\n" + str(result["text"]).strip() + "\n"
    if not streak.stopped and not guard_failed:
        for title in original_pending:
            for attempt in range(1, MAX_REPAIRS + 1):
                if not prev.needs_repair(text, doc_id, title):
                    break
                before = section_len(text, doc_id, title)
                attempts, result, anchor = guarded_call(
                    run_id=run_id,
                    doc_id=doc_id,
                    repeat=repeat,
                    phase="repair_pending",
                    round_index=attempt,
                    messages=repair_messages(text, doc_id, title, False),
                    cap=prev.repair_cap(text, doc_id, title),
                    current_tail=current_tail(text, doc_id, title),
                )
                calls.extend(attempts)
                streak.observe("repair_pending", anchor)
                if result is None:
                    repairs.append(
                        {
                            "phase": "repair_pending",
                            "title": title,
                            "attempt": attempt,
                            "before_chars": before,
                            "after_chars": before,
                            "accepted": False,
                        }
                    )
                    if attempts[-1]["guard_hits"]:
                        guard_failed = True
                    break
                text = prev.splice_section(text, doc_id, title, str(result["text"]))
                repairs.append(
                    {
                        "phase": "repair_pending",
                        "title": title,
                        "attempt": attempt,
                        "before_chars": before,
                        "after_chars": section_len(text, doc_id, title),
                        "accepted": True,
                        "anchor_lost": bool(anchor and anchor["anchor_lost"]),
                    }
                )
            if streak.stopped or guard_failed:
                break
    lost_count = sum(1 for event in streak.events if event["anchor_lost"])
    verdict = prev.judge(draft, text, doc_id, guard_failed)
    if lost_count:
        shapes = list(verdict["failure_shapes"])
        if "anchor_lost" not in shapes:
            shapes.append("anchor_lost")
        verdict["failure_shapes"] = shapes
        verdict["true_complete"] = False
    if streak.stopped:
        shapes = list(verdict["failure_shapes"])
        if "anchor_stop" not in shapes:
            shapes.append("anchor_stop")
        verdict["failure_shapes"] = shapes
        verdict["true_complete"] = False
    finalized = 0
    if verdict["true_complete"] and not verdict["has_ending"]:
        attempts, result, anchor = guarded_call(
            run_id=run_id,
            doc_id=doc_id,
            repeat=repeat,
            phase="finalize",
            round_index=1,
            messages=base.finalize_messages(),
            cap=prev.FINAL_CAP,
            current_tail=current_tail(text, doc_id, current_title),
        )
        calls.extend(attempts)
        streak.observe("finalize", anchor)
        finalized = 1
        if result is not None and str(result["text"]).strip() and not (anchor and anchor["anchor_lost"]):
            text = text.rstrip() + "\n\n" + str(result["text"]).strip() + "\n"
            verdict = prev.judge(draft, text, doc_id, False)
        elif anchor and anchor["anchor_lost"]:
            verdict["true_complete"] = False
            verdict["failure_shapes"] = list(verdict["failure_shapes"]) + ["anchor_lost"]
        elif attempts[-1]["guard_hits"]:
            verdict = prev.judge(draft, text, doc_id, True)
    lost_count = sum(1 for event in streak.events if event["anchor_lost"])
    store_run(
        doc_id=doc_id,
        repeat=repeat,
        draft=draft,
        text=text,
        calls=calls,
        repairs=repairs,
        anchors=streak.events,
        verdict=verdict,
        lost_count=lost_count,
        anchor_stopped=streak.stopped,
        phases={
            "finish_current": finish_rounds,
            "repair_current": current_repairs,
            "write_pending": pending_calls,
            "repair_pending": sum(1 for item in repairs if item["phase"] == "repair_pending"),
            "finalize": finalized,
            "guard_retry": sum(1 for item in calls if item.get("phase") == "guard_retry"),
        },
    )


def store_run(
    *,
    doc_id: str,
    repeat: int,
    draft: str,
    text: str,
    calls: list[dict[str, object]],
    repairs: list[dict[str, object]],
    anchors: list[dict[str, object]],
    verdict: dict[str, object],
    lost_count: int,
    anchor_stopped: bool,
    phases: dict[str, int],
) -> None:
    append(
        RUNS,
        {
            "run_id": f"{doc_id}-F-{repeat}",
            "strategy": "F",
            "doc_id": doc_id,
            "kind": base.DOCS[doc_id]["kind"],
            "repeat": repeat,
            "true_complete": bool(verdict["true_complete"]),
            "current_closed": bool(verdict["current_closed"]),
            "current_ending": verdict["current_ending"],
            "fragment_remains": bool(verdict["fragment_remains"]),
            "sections": verdict["sections"],
            "short_titles": verdict["short_titles"],
            "missing_titles": verdict["missing_titles"],
            "pending_ok": verdict["pending_ok"],
            "doc_chars": verdict["doc_chars"],
            "long_enough": bool(verdict["long_enough"]),
            "draft_overlap": verdict["draft_overlap"],
            "outline_overlap": verdict["outline_overlap"],
            "guard_hits": verdict["guard_hits"],
            "failure_shapes": verdict["failure_shapes"],
            "has_ending": bool(verdict["has_ending"]),
            "repairs": repairs,
            "anchors": anchors,
            "anchor_lost": lost_count,
            "anchor_stopped": anchor_stopped,
            "rejected_returns": sum(1 for item in calls if not item["accepted"]),
            "phases": phases,
            "prompt_tokens": sum(int(item["prompt_tokens"]) for item in calls),
            "completion_tokens": sum(int(item["completion_tokens"]) for item in calls),
            "total_tokens": sum(int(item["prompt_tokens"]) + int(item["completion_tokens"]) for item in calls),
            "elapsed_s": round(sum(float(item["elapsed_s"]) for item in calls), 3),
            "within_budget": all(bool(item.get("within_budget", True)) for item in calls),
            "continuation": verdict["continuation"],
        },
    )
    current_chars = next(
        (item["body_chars"] for item in verdict["sections"] if item["title"] == verdict.get("current", "")),
        None,
    )
    print(
        f"DONE {doc_id}-F-{repeat} true={verdict['true_complete']} "
        f"short={verdict['short_titles']} repairs={repairs} lost={lost_count} "
        f"chars={verdict['doc_chars']} current_chars={current_chars} shapes={verdict['failure_shapes']}",
        flush=True,
    )


def summarize() -> None:
    rows = [json.loads(line) for line in RUNS.read_text(encoding="utf-8").splitlines() if line.strip()]
    requests = [json.loads(line) for line in REQUESTS.read_text(encoding="utf-8").splitlines() if line.strip()]
    fresh = [item for item in requests if item.get("phase") != "draft"]
    diffs = [int(item["estimate_minus_actual"]) for item in fresh]
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        grouped.setdefault(str(row["doc_id"]), []).append(row)
    report: dict[str, object] = {
        "runs": len(rows),
        "fresh_requests": len(fresh),
        "within_budget": sum(1 for item in fresh if item["within_budget"]),
        "over_budget": sum(1 for item in fresh if not item["within_budget"]),
        "max_prompt_plus_max": max((int(item["prompt_plus_max"]) for item in fresh), default=0),
        "estimate_minus_actual": {
            "min": min(diffs) if diffs else 0,
            "max": max(diffs) if diffs else 0,
            "mean": round(sum(diffs) / len(diffs), 1) if diffs else 0,
        },
        "fresh_tokens": sum(int(item["prompt_tokens"]) + int(item["completion_tokens"]) for item in fresh),
        "fresh_elapsed_s": round(sum(float(item["elapsed_s"]) for item in fresh), 1),
        "anchor_lost": sum(int(row["anchor_lost"]) for row in rows),
        "repair_current": sum(1 for item in fresh if item.get("phase") == "repair_current"),
        "repair_pending": sum(1 for item in fresh if item.get("phase") == "repair_pending"),
        "section_floor": SECTION_FLOOR,
        "cells": {},
    }
    cells: dict[str, object] = {}
    for key, items in sorted(grouped.items()):
        count = len(items)
        cells[key] = {
            "n": count,
            "true_complete": sum(1 for item in items if item["true_complete"]),
            "current_closed": sum(1 for item in items if item["current_closed"]),
            "mean_doc_chars": round(sum(int(item["doc_chars"]) for item in items) / count, 1),
            "anchor_lost": sum(int(item["anchor_lost"]) for item in items),
            "anchor_stopped": sum(1 for item in items if item["anchor_stopped"]),
            "repairs": sum(len(item["repairs"]) for item in items),
            "guard_hits_in_doc": sum(1 for item in items if item["guard_hits"]),
            "failure_shapes": sorted({shape for item in items for shape in item["failure_shapes"]}),
            "mean_draft_overlap": round(sum(float(item["draft_overlap"]) for item in items) / count, 4),
            "mean_total_tokens": round(sum(int(item["total_tokens"]) for item in items) / count, 1),
            "mean_elapsed_s": round(sum(float(item["elapsed_s"]) for item in items) / count, 1),
        }
    report["cells"] = cells
    SUMMARY.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


def main() -> None:
    self_check()
    finished = done_ids()
    drafts = load_drafts()
    for doc_id in base.DOCS:
        for repeat in range(1, REPEATS + 1):
            if f"{doc_id}-F-{repeat}" in finished:
                continue
            run_f(doc_id, repeat, drafts[doc_id])
    summarize()


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPError as exc:
        raise SystemExit(f"llama-server 不可用：{exc}") from exc
