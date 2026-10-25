"""L0.10：拒绝泄漏后再补不足的节。不进产品路径。

F 仍是补当前节、一次 write_pending、通过后才 finalize。
任何返回命中思考标签、英文分析或 meta，整段拒绝，不写入正文。
重试一次，指令只有「只输出中文 Markdown 正文，不要思考过程/英文分析」。
重试仍泄漏则整次失败。不把泄漏段删掉再算完成。
write_pending 之后，只对没有标题或不足 450 字的未开始节补写，每节最多两次。
"""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
PRIOR_PATH = ROOT.parent / "continuation-spike-7" / "run_spike.py"
REQUESTS = ROOT / "requests.jsonl"
RUNS = ROOT / "runs.jsonl"
SUMMARY = ROOT / "summary.json"
DRAFT_SOURCE = ROOT.parent / "continuation-spike-7" / "requests.jsonl"

RETRY_INSTRUCTION = "只输出中文 Markdown 正文，不要思考过程/英文分析。"
FINISH_CAP = 1500
REPAIR_CAP = 1200
FINAL_CAP = 64
MAX_FINISH_ROUNDS = 2
MAX_REPAIRS = 2
REPEATS = 3


def load_prior():
    spec = importlib.util.spec_from_file_location("spike7", PRIOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("找不到 L0.9 脚本")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


base = load_prior()
TARGET_CHARS = base.TARGET_CHARS
SECTION_FLOOR = base.SECTION_FLOOR
LENGTH_FLOOR = base.LENGTH_FLOOR
CEILING = base.CEILING


def retry_messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    copied = [dict(item) for item in messages]
    if copied and copied[-1]["role"] == "user":
        copied[-1]["content"] = copied[-1]["content"] + "\n\n" + RETRY_INSTRUCTION
        return copied
    copied.append({"role": "user", "content": RETRY_INSTRUCTION})
    return copied


def drop_repeated_heading(title: str, text: str) -> str:
    lines = text.strip().splitlines()
    if lines and lines[0].strip() == f"## {title}":
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines).strip()


def splice_section(text: str, doc_id: str, title: str, addition: str) -> str:
    addition = drop_repeated_heading(title, addition)
    if not addition:
        return text
    lines = text.splitlines()
    marks = base.heading_marks(text, doc_id)
    positions = [index for index, name in marks if name == title]
    if not positions:
        return text.rstrip() + "\n\n" + addition + "\n"
    start = positions[-1]
    end = len(lines)
    for index, _name in marks:
        if index > start:
            end = index
            break
    body_end = end
    while body_end > start + 1 and not lines[body_end - 1].strip():
        body_end -= 1
    return ("\n".join(lines[:body_end] + ["", addition, ""] + lines[end:])).rstrip() + "\n"


def continuation_from(draft: str, text: str) -> str:
    if not text.startswith(draft):
        raise RuntimeError("正文离开了初稿")
    return text[len(draft) :].lstrip("\n")


def repair_messages(text: str, doc_id: str, title: str) -> list[dict[str, str]]:
    body = base.last_bodies(text, doc_id).get(title, "")
    gap = max(0, TARGET_CHARS - len(body.strip()))
    user = "\n".join(
        [
            f"这一节：## {title}",
            f"要点：{base.point_of(doc_id, title)}",
            f"目标剩余字数：{gap}",
            "只补完这一节，不写下一节，不写「完」。",
        ]
    )
    messages: list[dict[str, str]] = [
        {"role": "system", "content": base.SYSTEM},
        {"role": "user", "content": user},
    ]
    tail = base.clip_tail(body, 80) if body.strip() else ""
    if tail:
        messages.append({"role": "assistant", "content": tail})
        messages.append({"role": "user", "content": "只从最后一个字之后继续。只补完这一节，不写下一节，不写「完」。"})
    return messages


def repair_cap(text: str, doc_id: str, title: str) -> int:
    body = base.last_bodies(text, doc_id).get(title, "")
    gap = max(SECTION_FLOOR - len(body.strip()), 200)
    return min(REPAIR_CAP, gap * 3 + 200)


def judge(draft: str, text: str, doc_id: str, guard_failed: bool) -> dict[str, object]:
    continuation = continuation_from(draft, text)
    merged = draft + ("\n\n" + continuation if continuation else "")
    current = base.present_headings(draft, doc_id)[-1]
    pending = base.unfinished(draft, doc_id)
    draft_body = base.first_body(draft, current, doc_id)
    merged_body = base.first_body(merged, current, doc_id)
    added = len(merged_body) - len(draft_body)
    current_closed = base.sentence_closed(merged_body) and added >= base.MIN_CURRENT_ADDED
    fragment = draft.rstrip().splitlines()[-1].strip()
    fragment_remains = bool(fragment) and fragment in merged and not fragment.endswith(tuple(base.CLOSERS))
    bodies = base.last_bodies(merged, doc_id)
    per_section: list[dict[str, object]] = []
    short_titles: list[str] = []
    missing_titles: list[str] = []
    for title in base.headings_of(doc_id):
        body = bodies.get(title, "")
        point = base.point_of(doc_id, title)
        body_chars = len(body.strip())
        copied = base.overlap_ratio(point, body) if body else 0.0
        closed = base.sentence_closed(body)
        present = title in bodies
        at_floor = present and body_chars >= SECTION_FLOOR and closed and copied < 0.5
        if not present:
            missing_titles.append(title)
        elif body_chars < SECTION_FLOOR or not closed:
            short_titles.append(title)
        per_section.append(
            {
                "title": title,
                "body_chars": body_chars,
                "outline_overlap": round(copied, 4),
                "closed": closed,
                "present": present,
                "at_floor": at_floor,
                "pending": title in pending,
            }
        )
    guard = base.output_guard(continuation)
    draft_overlap = round(base.overlap_ratio(draft, continuation), 4)
    outline_overlap = round(base.overlap_ratio(base.outline_of(doc_id), continuation), 4)
    early = base.early_done_count(merged, doc_id)
    open_sections = [title for title, body in base.spans(merged, doc_id) if not base.sentence_closed(body)]
    long_enough = len(merged) >= LENGTH_FLOOR
    shapes: list[str] = []
    if guard_failed or guard["guard_hits"]:
        shapes.append("guard_rejected")
    shapes.extend(str(hit) for hit in guard["guard_hits"])
    if not current_closed:
        shapes.append("current_open")
    if missing_titles:
        shapes.append("missing_heading")
    if short_titles or any(not item["at_floor"] for item in per_section):
        shapes.append("short_section")
    if open_sections:
        shapes.append("open_section")
    if early:
        shapes.append("early_done")
    if not long_enough:
        shapes.append("short_doc")
    if outline_overlap >= 0.5 or any(float(item["outline_overlap"]) >= 0.5 for item in per_section):
        shapes.append("outline_overlap")
    if draft_overlap >= 0.5:
        shapes.append("draft_overlap")
    pending_ok = sum(1 for item in per_section if item["pending"] and item["at_floor"])
    return {
        "current_closed": current_closed,
        "current_ending": (base.content_lines(merged_body) or [""])[-1][-40:],
        "fragment_remains": fragment_remains,
        "sections": per_section,
        "short_titles": short_titles,
        "missing_titles": missing_titles,
        "pending_ok": pending_ok,
        "pending_total": len(pending),
        "open_sections": open_sections,
        "early_done": early,
        "doc_chars": len(merged),
        "long_enough": long_enough,
        "draft_overlap": draft_overlap,
        "outline_overlap": outline_overlap,
        "guard_hits": guard["guard_hits"],
        "meta_terms": guard["meta_terms"],
        "english_chars": guard["english_chars"],
        "failure_shapes": shapes,
        "true_complete": not shapes,
        "has_ending": merged.strip().endswith("完"),
        "continuation": continuation,
    }


def self_check() -> None:
    assert SECTION_FLOOR == 450
    assert RETRY_INSTRUCTION == "只输出中文 Markdown 正文，不要思考过程/英文分析。"
    leaked = (
        "<think>\nThe user wants an analysis. Let me look at the draft.\n\n"
        + ("This paragraph keeps analyzing the previous section in English. " * 8)
        + "\n</think>\n\n中文正文。"
    )
    assert base.output_guard(leaked)["guard_hits"]
    stripped = base.THINK_TAG.sub("", leaked)
    assert "<think>" not in stripped and base.output_guard(stripped)["guard_hits"]
    assert base.output_guard("把 max_tokens 和 n_ctx 放在同一段里。")["guard_hits"] == []
    doc_id = "retro"
    window = "目标已经写完。" * 80
    draft = f"## 目标\n{window}\n\n## 过程\n过程写到一半，风格引导"
    ready = "这一节已经写够。" * 70
    short = (
        "## 结果\n" + ("结果还短。" * 8) + "\n\n"
        "## 原因\n" + ready + "\n\n"
        "## 改法\n" + ready + "\n\n"
        "## 下一步\n" + ready
    )
    text = draft + "\n\n" + ("过程补成完整段落。" * 70) + "\n\n" + short
    before = len(base.last_bodies(text, doc_id)["原因"])
    repaired = splice_section(text, doc_id, "结果", "## 结果\n" + ("结果补到目标长度。" * 60))
    assert "<think>" not in repaired
    assert len(base.last_bodies(repaired, doc_id)["结果"]) >= SECTION_FLOOR
    assert len(base.last_bodies(repaired, doc_id)["原因"]) == before
    verdict = judge(draft, repaired, doc_id, guard_failed=False)
    assert verdict["true_complete"], verdict["failure_shapes"]
    leaked_doc = splice_section(repaired, doc_id, "结果", leaked)
    assert not judge(draft, leaked_doc, doc_id, guard_failed=False)["true_complete"]
    assert not judge(draft, text, doc_id, guard_failed=False)["true_complete"]
    assert judge(draft, repaired, doc_id, guard_failed=True)["failure_shapes"][0] == "guard_rejected"
    assert base.estimate("甲" * 2000) == 2000
    messages = repair_messages(text, doc_id, "结果")
    assert messages[-1]["content"].endswith("不写「完」。")
    retried = retry_messages(messages)
    assert retried[-1]["content"].endswith(RETRY_INSTRUCTION)
    assert leaked not in retried[-1]["content"]


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
    result.update(
        {
            "phase": phase,
            "max_tokens": max_tokens,
            "within_budget": within,
            "accepted": accepted,
            "guard_hits": guard["guard_hits"],
            "meta_terms": guard["meta_terms"],
            "english_chars": guard["english_chars"],
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
        "messages": messages,
        "text": result["text"],
    },
    )
    print(
        f"  tokens={actual}+{result['completion_tokens']} truncated={result['truncated']} "
        f"accepted={accepted} guard={guard['guard_hits'] or '-'} {result['elapsed_s']}s",
        flush=True,
    )
    return result


def guarded_call(**kwargs: object) -> tuple[list[dict[str, object]], dict[str, object] | None]:
    messages = kwargs["messages"]
    assert isinstance(messages, list)
    attempts: list[dict[str, object]] = []
    first = call(**kwargs)  # type: ignore[arg-type]
    attempts.append(first)
    if first["accepted"]:
        return attempts, first
    if not first["guard_hits"]:
        return attempts, None
    second = call(
        run_id=kwargs["run_id"],  # type: ignore[arg-type]
        doc_id=kwargs["doc_id"],  # type: ignore[arg-type]
        repeat=kwargs["repeat"],  # type: ignore[arg-type]
        phase="guard_retry",
        round_index=kwargs["round_index"],  # type: ignore[arg-type]
        messages=retry_messages(messages),
        cap=kwargs["cap"],  # type: ignore[arg-type]
        attempt=2,
        retried_phase=str(kwargs["phase"]),
    )
    attempts.append(second)
    return attempts, second if second["accepted"] else None


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
                "reused_from": "continuation-spike-7",
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


def needs_repair(text: str, doc_id: str, title: str) -> bool:
    if title not in base.present_headings(text, doc_id):
        return True
    body = base.last_bodies(text, doc_id).get(title, "")
    return len(body.strip()) < SECTION_FLOOR or not base.sentence_closed(body)


def run_f(doc_id: str, repeat: int, draft: str) -> None:
    run_id = f"{doc_id}-F-{repeat}"
    calls: list[dict[str, object]] = []
    repairs: list[dict[str, object]] = []
    text = draft
    guard_failed = False
    finish_rounds = 0
    current_title = base.present_headings(draft, doc_id)[-1]
    for _attempt in range(MAX_FINISH_ROUNDS):
        if base.open_section(text, doc_id)[0] is None:
            break
        attempts, result = guarded_call(
            run_id=run_id,
            doc_id=doc_id,
            repeat=repeat,
            phase="finish_current",
            round_index=finish_rounds + 1,
            messages=base.finish_messages(text, doc_id),
            cap=FINISH_CAP,
            attempt=1,
        )
        calls.extend(attempts)
        finish_rounds += 1
        if result is None:
            if attempts[-1]["guard_hits"]:
                guard_failed = True
            break
        text = text.rstrip() + "\n\n" + str(result["text"]).strip() + "\n"
        if base.sentence_closed(base.first_body(text, current_title, doc_id)):
            break
    pending_calls = 0
    original_pending = base.unfinished(draft, doc_id)
    if not guard_failed:
        missing = [title for title in original_pending if needs_repair(text, doc_id, title)]
        if missing:
            attempts, result = guarded_call(
                run_id=run_id,
                doc_id=doc_id,
                repeat=repeat,
                phase="write_pending",
                round_index=1,
                messages=base.pending_batch_messages(text, doc_id, missing),
                cap=base.pending_output_cap(len(missing)),
                attempt=1,
            )
            calls.extend(attempts)
            pending_calls = 1
            if result is None:
                if attempts[-1]["guard_hits"]:
                    guard_failed = True
            else:
                text = text.rstrip() + "\n\n" + str(result["text"]).strip() + "\n"
    if not guard_failed:
        for title in original_pending:
            for attempt in range(1, MAX_REPAIRS + 1):
                if not needs_repair(text, doc_id, title):
                    break
                before = len(base.last_bodies(text, doc_id).get(title, "").strip())
                attempts, result = guarded_call(
                    run_id=run_id,
                    doc_id=doc_id,
                    repeat=repeat,
                    phase="repair_section",
                    round_index=attempt,
                    messages=repair_messages(text, doc_id, title),
                    cap=repair_cap(text, doc_id, title),
                    attempt=1,
                )
                calls.extend(attempts)
                if result is None:
                    repairs.append(
                        {
                            "title": title,
                            "attempt": attempt,
                            "before_chars": before,
                            "after_chars": before,
                            "accepted": False,
                            "guard_hits": attempts[-1]["guard_hits"],
                        }
                    )
                    if attempts[-1]["guard_hits"]:
                        guard_failed = True
                    break
                text = splice_section(text, doc_id, title, str(result["text"]))
                after = len(base.last_bodies(text, doc_id).get(title, "").strip())
                repairs.append(
                    {
                        "title": title,
                        "attempt": attempt,
                        "before_chars": before,
                        "after_chars": after,
                        "accepted": True,
                        "guard_hits": [],
                    }
                )
            if guard_failed:
                break
    verdict = judge(draft, text, doc_id, guard_failed)
    finalized = 0
    if (
        not guard_failed
        and verdict["true_complete"]
        and not verdict["has_ending"]
    ):
        attempts, result = guarded_call(
            run_id=run_id,
            doc_id=doc_id,
            repeat=repeat,
            phase="finalize",
            round_index=1,
            messages=base.finalize_messages(),
            cap=FINAL_CAP,
            attempt=1,
        )
        calls.extend(attempts)
        finalized = 1
        if result is not None and str(result["text"]).strip():
            text = text.rstrip() + "\n\n" + str(result["text"]).strip() + "\n"
            verdict = judge(draft, text, doc_id, False)
        elif attempts[-1]["guard_hits"]:
            guard_failed = True
            verdict = judge(draft, text, doc_id, True)
    store_run(
        doc_id=doc_id,
        repeat=repeat,
        draft=draft,
        text=text,
        calls=calls,
        repairs=repairs,
        verdict=verdict,
        phases={
            "finish_current": finish_rounds,
            "guard_retry": sum(1 for item in calls if item.get("phase") == "guard_retry"),
            "write_pending": pending_calls,
            "repair_section": len(repairs),
            "finalize": finalized,
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
    verdict: dict[str, object],
    phases: dict[str, int],
) -> None:
    prompt_tokens = sum(int(item["prompt_tokens"]) for item in calls)
    completion_tokens = sum(int(item["completion_tokens"]) for item in calls)
    rejected = sum(1 for item in calls if not item["accepted"])
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
            "pending_total": verdict["pending_total"],
            "open_sections": verdict["open_sections"],
            "early_done": verdict["early_done"],
            "doc_chars": verdict["doc_chars"],
            "long_enough": bool(verdict["long_enough"]),
            "draft_overlap": verdict["draft_overlap"],
            "outline_overlap": verdict["outline_overlap"],
            "guard_hits": verdict["guard_hits"],
            "meta_terms": verdict["meta_terms"],
            "english_chars": verdict["english_chars"],
            "failure_shapes": verdict["failure_shapes"],
            "has_ending": bool(verdict["has_ending"]),
            "repairs": repairs,
            "rejected_returns": rejected,
            "phases": phases,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "elapsed_s": round(sum(float(item["elapsed_s"]) for item in calls), 3),
            "within_budget": all(bool(item.get("within_budget", True)) for item in calls),
            "continuation": verdict["continuation"],
        },
    )
    print(
        f"DONE {doc_id}-F-{repeat} true={verdict['true_complete']} "
        f"closed={verdict['current_closed']} pending={verdict['pending_ok']}/{verdict['pending_total']} "
        f"short={verdict['short_titles']} repairs={len(repairs)} rejected={rejected} "
        f"chars={verdict['doc_chars']} shapes={verdict['failure_shapes']}",
        flush=True,
    )


def summarize() -> None:
    rows = [json.loads(line) for line in RUNS.read_text(encoding="utf-8").splitlines() if line.strip()]
    requests = [json.loads(line) for line in REQUESTS.read_text(encoding="utf-8").splitlines() if line.strip()]
    fresh = [item for item in requests if item.get("phase") != "draft"]
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        grouped.setdefault(str(row["doc_id"]), []).append(row)
    diffs = [int(item["estimate_minus_actual"]) for item in fresh]
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
        "rejected_returns": sum(1 for item in fresh if not item.get("accepted", True)),
        "guard_retries": sum(1 for item in fresh if item.get("phase") == "guard_retry"),
        "repair_requests": sum(1 for item in fresh if item.get("phase") == "repair_section"),
        "section_floor": SECTION_FLOOR,
        "length_floor": LENGTH_FLOOR,
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
            "long_enough": sum(1 for item in items if item["long_enough"]),
            "mean_draft_overlap": round(sum(float(item["draft_overlap"]) for item in items) / count, 4),
            "mean_outline_overlap": round(sum(float(item["outline_overlap"]) for item in items) / count, 4),
            "early_done": sum(int(item["early_done"]) for item in items),
            "rejected_returns": sum(int(item["rejected_returns"]) for item in items),
            "repairs": sum(len(item["repairs"]) for item in items),
            "guard_hits_in_doc": sum(1 for item in items if item["guard_hits"]),
            "failure_shapes": sorted({shape for item in items for shape in item["failure_shapes"]}),
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
        draft = drafts[doc_id]
        for repeat in range(1, REPEATS + 1):
            run_id = f"{doc_id}-F-{repeat}"
            if run_id in finished:
                continue
            run_f(doc_id, repeat, draft)
    summarize()


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPError as exc:
        raise SystemExit(f"llama-server 不可用：{exc}") from exc
