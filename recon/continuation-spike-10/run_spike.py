"""L0.12：补写不能靠重复凑字数；锚点丢失或泄漏则改写当前节。不进产品路径。

repair 的新增文本若有一半以上的 12 字来自本节已有正文，或再次出现同一结尾句，整段不写入。
再用「只写新的句子，不要重复已有内容」重试一次。仍重复则这次失败。
<think> 泄漏、重试仍泄漏、或 anchor_lost 时，不再从尾部接，改为重写并替换当前节。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
PRIOR_PATH = ROOT.parent / "continuation-spike-9" / "run_spike.py"
REQUESTS = ROOT / "requests.jsonl"
RUNS = ROOT / "runs.jsonl"
SUMMARY = ROOT / "summary.json"
DRAFT_SOURCE = ROOT.parent / "continuation-spike-9" / "requests.jsonl"
NOVELTY_MAX = 0.5
NOVELTY_RETRY = "只写新的句子，不要重复已有内容。"
REPEATS = 3
REWRITE_CAP = 1500


def load_prior():
    spec = importlib.util.spec_from_file_location("spike9", PRIOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("找不到 L0.11 脚本")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s9 = load_prior()
base = s9.base
s8 = s9.prev
SECTION_FLOOR = s9.SECTION_FLOOR
CEILING = s9.CEILING


def ending_sentence(body: str) -> str:
    compact = "".join(base.content_lines(body))
    parts: list[str] = []
    buf = ""
    for char in compact:
        buf += char
        if char in base.CLOSERS:
            parts.append(buf.strip())
            buf = ""
    if parts:
        return parts[-1]
    lines = base.content_lines(body)
    return lines[-1] if lines else ""


def novelty_check(existing: str, addition: str) -> dict[str, object]:
    addition = addition.strip()
    overlap = base.overlap_ratio(existing, addition) if addition else 1.0
    ending = ending_sentence(existing)
    repeated = bool(ending) and len(ending) >= 8 and ending in addition
    return {
        "novelty_overlap": round(overlap, 4),
        "repeated_ending": repeated,
        "novelty_rejected": overlap >= NOVELTY_MAX or repeated or not addition,
    }


def replace_section(text: str, doc_id: str, title: str, new_body: str) -> str:
    new_body = s8.drop_repeated_heading(title, new_body)
    lines = text.splitlines()
    marks = base.heading_marks(text, doc_id)
    positions = [index for index, name in marks if name == title]
    if not positions:
        return text.rstrip() + f"\n\n## {title}\n\n{new_body}\n"
    start = positions[-1]
    end = len(lines)
    for index, _name in marks:
        if index > start:
            end = index
            break
    return ("\n".join(lines[: start + 1] + ["", new_body, ""] + lines[end:])).rstrip() + "\n"


def previous_summary(text: str, doc_id: str, title: str) -> str:
    headings = base.headings_of(doc_id)
    index = headings.index(title)
    if index == 0:
        return "（没有上一节）"
    previous = headings[index - 1]
    body = base.last_bodies(text, doc_id).get(previous, "")
    return base.paragraph_digest(body) if body else "（空）"


def rewrite_messages(text: str, doc_id: str, title: str) -> list[dict[str, str]]:
    user = "\n".join(
        [
            f"重写当前节：## {title}",
            f"要点：{base.point_of(doc_id, title)}",
            f"上一节摘要：{previous_summary(text, doc_id, title)}",
            f"目标字数：{s9.prev.TARGET_CHARS}",
            "只输出这一节的完整正文。不要写其他标题，不要写「完」，不要写思考过程。",
        ]
    )
    return [{"role": "system", "content": base.SYSTEM}, {"role": "user", "content": user}]


def rewrite_ok(old_body: str, new_body: str, doc_id: str, title: str) -> dict[str, object]:
    body = s8.drop_repeated_heading(title, new_body).strip()
    guard = base.output_guard(body)
    old_overlap = base.overlap_ratio(old_body, body) if body else 1.0
    point_overlap = base.overlap_ratio(base.point_of(doc_id, title), body) if body else 1.0
    closed = base.sentence_closed(body)
    long = len(body) >= SECTION_FLOOR
    ok = not guard["guard_hits"] and closed and long and old_overlap < 0.5 and point_overlap < 0.5
    return {
        "ok": ok,
        "body_chars": len(body),
        "closed": closed,
        "guard_hits": guard["guard_hits"],
        "old_overlap": round(old_overlap, 4),
        "outline_overlap": round(point_overlap, 4),
    }


def new_material(draft: str, text: str, doc_id: str) -> str:
    if text.startswith(draft):
        return text[len(draft) :].lstrip("\n")
    parts: list[str] = []
    current = base.present_headings(draft, doc_id)[-1]
    parts.append(base.last_bodies(text, doc_id).get(current, ""))
    for title in base.unfinished(draft, doc_id):
        parts.append(base.last_bodies(text, doc_id).get(title, ""))
    return "\n".join(part for part in parts if part)


def judge_doc(draft: str, text: str, doc_id: str, guard_failed: bool) -> dict[str, object]:
    if text.startswith(draft):
        return s8.judge(draft, text, doc_id, guard_failed)
    merged = text
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
    material = new_material(draft, text, doc_id)
    guard = base.output_guard(merged)
    draft_overlap = round(base.overlap_ratio(draft, material), 4)
    outline_overlap = round(base.overlap_ratio(base.outline_of(doc_id), material), 4)
    early = base.early_done_count(merged, doc_id)
    open_sections = [title for title, body in base.spans(merged, doc_id) if not base.sentence_closed(body)]
    long_enough = len(merged) >= s8.LENGTH_FLOOR
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
    return {
        "current_closed": current_closed,
        "current_ending": (base.content_lines(merged_body) or [""])[-1][-40:],
        "fragment_remains": fragment_remains,
        "sections": per_section,
        "short_titles": short_titles,
        "missing_titles": missing_titles,
        "pending_ok": sum(1 for item in per_section if item["pending"] and item["at_floor"]),
        "pending_total": len(pending),
        "open_sections": open_sections,
        "early_done": early,
        "doc_chars": len(merged),
        "long_enough": long_enough,
        "draft_overlap": draft_overlap,
        "outline_overlap": outline_overlap,
        "guard_hits": guard["guard_hits"],
        "failure_shapes": shapes,
        "true_complete": not shapes,
        "has_ending": merged.strip().endswith("完"),
        "continuation": material,
    }


def self_check() -> None:
    existing = "建议预留缓冲。确保推理过程不因溢出而失败。"
    bad = novelty_check(existing, "确保推理过程不因溢出而失败。")
    assert bad["novelty_rejected"] and bad["repeated_ending"]
    longer = "留 10%–20% 的缓冲余量，以应对波动，确保推理过程不因溢出而失败。"
    assert novelty_check(existing, longer)["repeated_ending"]
    assert novelty_check(existing, existing)["novelty_overlap"] >= NOVELTY_MAX
    fresh = "另外把安全余量写成检查项，每次改 prompt 后都重算一次。"
    good = novelty_check(existing, fresh)
    assert not good["novelty_rejected"]
    doc_id = "tech"
    draft = "## 窗口\n" + ("窗口已经写完。" * 70) + "\n\n## 余量\n余量写到一半，总需求为"
    new_body = "余量按窗口减去 prompt 和输出来留。" * 40
    replaced = replace_section(draft, doc_id, "余量", "## 余量\n" + new_body)
    assert "总需求为" not in base.last_bodies(replaced, doc_id)["余量"]
    assert "窗口已经写完。" in replaced
    assert len(base.last_bodies(replaced, doc_id)["余量"]) >= SECTION_FLOOR
    verdict = judge_doc(draft, replaced, doc_id, False)
    assert verdict["current_closed"]
    leaked = "<think>\nLet me analysis this section in English. " + ("thinking about the draft. " * 12) + "</think>"
    assert not rewrite_ok(base.first_body(draft, "余量", doc_id), leaked, doc_id, "余量")["ok"]
    assert rewrite_ok(base.first_body(draft, "余量", doc_id), new_body, doc_id, "余量")["ok"]
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


def call(run_id: str, doc_id: str, repeat: int, phase: str, round_index: int, messages: list[dict[str, str]], cap: int, attempt: int, retried_phase: str = "", extra: dict[str, object] | None = None) -> dict[str, object]:
    max_tokens, estimate_prompt = base.output_budget(messages, cap)
    print(f"{run_id} {phase} a{attempt} r{round_index} estimate={estimate_prompt} max_tokens={max_tokens}", flush=True)
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
    extra = extra or {}
    result.update({"phase": phase, "accepted": accepted, "guard_hits": guard["guard_hits"], "within_budget": within, "max_tokens": max_tokens})
    row = {
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
        "elapsed_s": result["elapsed_s"],
        "estimate_minus_actual": estimate_prompt - actual,
        "within_budget": within,
        "prompt_plus_max": actual + max_tokens,
        "ceiling": CEILING,
        "accepted": accepted,
        "guard_hits": guard["guard_hits"],
        "retried_phase": retried_phase,
        "messages": messages,
        "text": result["text"],
    }
    row.update(extra)
    append(REQUESTS, row)
    print(
        f"  tokens={actual}+{result['completion_tokens']} accepted={accepted} "
        f"guard={guard['guard_hits'] or '-'} extra={ {k: extra[k] for k in extra if k != 'text'} } {result['elapsed_s']}s",
        flush=True,
    )
    return result


def guarded_finish(run_id: str, doc_id: str, repeat: int, phase: str, round_index: int, messages: list[dict[str, str]], cap: int, current_tail: str) -> tuple[list[dict[str, object]], dict[str, object] | None, dict[str, object] | None]:
    first = call(run_id, doc_id, repeat, phase, round_index, messages, cap, 1)
    attempts = [first]
    if first["accepted"]:
        return attempts, first, None
    if not first["guard_hits"]:
        return attempts, None, None
    second = call(run_id, doc_id, repeat, "guard_retry", round_index, s9.prev.retry_messages(messages), cap, 2, phase)
    anchor = s9.anchor_check(current_tail, str(first["text"]), str(second["text"]))
    second["anchor"] = anchor
    _patch_last(anchor)
    attempts.append(second)
    if second["accepted"] and not anchor["anchor_lost"]:
        return attempts, second, anchor
    return attempts, None, anchor


def _patch_last(fields: dict[str, object]) -> None:
    lines = REQUESTS.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[-1])
    row.update(fields)
    lines[-1] = json.dumps(row, ensure_ascii=False)
    REQUESTS.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_drafts() -> dict[str, str]:
    if REQUESTS.exists():
        found: dict[str, str] = {}
        for line in REQUESTS.read_text(encoding="utf-8").splitlines():
            if line.strip() and json.loads(line).get("phase") == "draft" and json.loads(line).get("draft_ok"):
                item = json.loads(line)
                found[str(item["doc_id"])] = str(item["text"])
        if len(found) == len(base.DOCS):
            return found
    prior = [json.loads(line) for line in DRAFT_SOURCE.read_text(encoding="utf-8").splitlines() if line.strip()]
    loaded: dict[str, str] = {}
    for doc_id in base.DOCS:
        source = next(item for item in prior if item.get("phase") == "draft" and item.get("draft_ok") and item["doc_id"] == doc_id)
        text = str(source["text"]).rstrip()
        current = base.present_headings(text, doc_id)[-1]
        if base.sentence_closed(base.first_body(text, current, doc_id)):
            raise SystemExit(f"{doc_id} 当前节不是半句")
        print(f"{doc_id} current={current} chars={len(text)} end={text[-24:]!r}", flush=True)
        append(REQUESTS, {"run_id": f"{doc_id}-draft", "strategy": "draft", "doc_id": doc_id, "phase": "draft", "draft_ok": True, "reused_from": "continuation-spike-9", "text": text, "within_budget": True, "accepted": True, "elapsed_s": 0, "prompt_tokens": 0, "completion_tokens": 0})
        loaded[doc_id] = text
    return loaded


def section_len(text: str, doc_id: str, title: str) -> int:
    return len(base.last_bodies(text, doc_id).get(title, "").strip())


def novelty_generate(run_id: str, doc_id: str, repeat: int, phase: str, round_index: int, messages: list[dict[str, str]], cap: int, existing: str) -> tuple[list[dict[str, object]], str | None, dict[str, object]]:
    calls: list[dict[str, object]] = []
    first = call(run_id, doc_id, repeat, phase, round_index, messages, cap, 1)
    calls.append(first)
    if not first["accepted"]:
        return calls, None, {"novelty_rejected": False, "guard_hits": first["guard_hits"]}
    check = novelty_check(existing, str(first["text"]))
    _patch_last(check)
    if not check["novelty_rejected"]:
        return calls, str(first["text"]), check
    retried_messages = [dict(item) for item in messages]
    retried_messages[-1]["content"] = retried_messages[-1]["content"] + "\n\n" + NOVELTY_RETRY
    second = call(run_id, doc_id, repeat, "novelty_retry", round_index, retried_messages, cap, 2, phase)
    calls.append(second)
    if not second["accepted"]:
        return calls, None, {"novelty_rejected": True, "guard_hits": second["guard_hits"]}
    check = novelty_check(existing, str(second["text"]))
    _patch_last(check)
    if check["novelty_rejected"]:
        return calls, None, check
    return calls, str(second["text"]), check


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
    finish_rounds = 0
    for _attempt in range(s8.MAX_FINISH_ROUNDS):
        if failed or base.open_section(text, doc_id)[0] is None:
            break
        attempts, result, anchor = guarded_finish(
            run_id, doc_id, repeat, "finish_current", finish_rounds + 1,
            base.finish_messages(text, doc_id), s8.FINISH_CAP, s9.current_tail(text, doc_id, current_title),
        )
        calls.extend(attempts)
        finish_rounds += 1
        if anchor and anchor["anchor_lost"]:
            anchor_lost += 1
        if result is None:
            leaked = any(item.get("guard_hits") for item in attempts)
            lost = bool(anchor and anchor["anchor_lost"])
            if leaked or lost:
                old = base.last_bodies(text, doc_id).get(current_title, "")
                rewritten = call(run_id, doc_id, repeat, "rewrite_current", 1, rewrite_messages(text, doc_id, current_title), REWRITE_CAP, 1)
                calls.append(rewritten)
                report = rewrite_ok(old, str(rewritten["text"]), doc_id, current_title)
                _patch_last(report)
                rewrites.append(report)
                if report["ok"]:
                    text = replace_section(text, doc_id, current_title, str(rewritten["text"]))
                else:
                    failed = True
                    fail_shape = "rewrite_rejected"
            else:
                failed = True
                fail_shape = "finish_empty"
            break
        text = text.rstrip() + "\n\n" + str(result["text"]).strip() + "\n"
        if base.sentence_closed(base.first_body(text, current_title, doc_id)):
            break
    current_repairs = 0
    while not failed and s8.needs_repair(text, doc_id, current_title) and current_repairs < s9.MAX_REPAIRS:
        before = section_len(text, doc_id, current_title)
        existing = base.last_bodies(text, doc_id).get(current_title, "")
        generated, addition, check = novelty_generate(
            run_id, doc_id, repeat, "repair_current", current_repairs + 1,
            s9.repair_messages(text, doc_id, current_title, True), s8.repair_cap(text, doc_id, current_title), existing,
        )
        calls.extend(generated)
        current_repairs += 1
        if addition is None:
            repairs.append({"phase": "repair_current", "title": current_title, "attempt": current_repairs, "before_chars": before, "after_chars": before, "accepted": False, **check})
            failed = True
            fail_shape = "guard_rejected" if check.get("guard_hits") else "novelty_rejected"
            break
        text = s8.splice_section(text, doc_id, current_title, addition)
        repairs.append({"phase": "repair_current", "title": current_title, "attempt": current_repairs, "before_chars": before, "after_chars": section_len(text, doc_id, current_title), "accepted": True, **check})
    pending_calls = 0
    original_pending = base.unfinished(draft, doc_id)
    if not failed:
        missing = [title for title in original_pending if s8.needs_repair(text, doc_id, title)]
        if missing:
            result = call(run_id, doc_id, repeat, "write_pending", 1, base.pending_batch_messages(text, doc_id, missing), base.pending_output_cap(len(missing)), 1)
            calls.append(result)
            pending_calls = 1
            if not result["accepted"]:
                failed = True
                fail_shape = "guard_rejected"
            else:
                text = text.rstrip() + "\n\n" + str(result["text"]).strip() + "\n"
    if not failed:
        for title in original_pending:
            for attempt in range(1, s9.MAX_REPAIRS + 1):
                if not s8.needs_repair(text, doc_id, title):
                    break
                before = section_len(text, doc_id, title)
                existing = base.last_bodies(text, doc_id).get(title, "")
                generated, addition, check = novelty_generate(
                    run_id, doc_id, repeat, "repair_pending", attempt,
                    s9.repair_messages(text, doc_id, title, False), s8.repair_cap(text, doc_id, title), existing,
                )
                calls.extend(generated)
                if addition is None:
                    repairs.append({"phase": "repair_pending", "title": title, "attempt": attempt, "before_chars": before, "after_chars": before, "accepted": False, **check})
                    failed = True
                    fail_shape = "guard_rejected" if check.get("guard_hits") else "novelty_rejected"
                    break
                text = s8.splice_section(text, doc_id, title, addition)
                repairs.append({"phase": "repair_pending", "title": title, "attempt": attempt, "before_chars": before, "after_chars": section_len(text, doc_id, title), "accepted": True, **check})
            if failed:
                break
    verdict = judge_doc(draft, text, doc_id, failed and fail_shape == "guard_rejected")
    if failed and fail_shape and fail_shape not in verdict["failure_shapes"]:
        verdict["failure_shapes"] = list(verdict["failure_shapes"]) + [fail_shape]
        verdict["true_complete"] = False
    finalized = 0
    if verdict["true_complete"] and not verdict["has_ending"] and not failed:
        result = call(run_id, doc_id, repeat, "finalize", 1, base.finalize_messages(), s8.FINAL_CAP, 1)
        calls.append(result)
        finalized = 1
        if result["accepted"] and str(result["text"]).strip():
            text = text.rstrip() + "\n\n" + str(result["text"]).strip() + "\n"
            verdict = judge_doc(draft, text, doc_id, False)
        elif result["guard_hits"]:
            verdict = judge_doc(draft, text, doc_id, True)
    store_run(doc_id, repeat, draft, text, calls, repairs, rewrites, verdict, anchor_lost, {
        "finish_current": finish_rounds,
        "repair_current": current_repairs,
        "write_pending": pending_calls,
        "repair_pending": sum(1 for item in repairs if item["phase"] == "repair_pending"),
        "rewrite_current": len(rewrites),
        "finalize": finalized,
    })


def store_run(doc_id: str, repeat: int, draft: str, text: str, calls: list[dict[str, object]], repairs: list[dict[str, object]], rewrites: list[dict[str, object]], verdict: dict[str, object], anchor_lost: int, phases: dict[str, int]) -> None:
    append(RUNS, {
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
        "doc_chars": verdict["doc_chars"],
        "long_enough": bool(verdict["long_enough"]),
        "draft_overlap": verdict["draft_overlap"],
        "outline_overlap": verdict["outline_overlap"],
        "guard_hits": verdict["guard_hits"],
        "failure_shapes": verdict["failure_shapes"],
        "has_ending": bool(verdict["has_ending"]),
        "repairs": repairs,
        "rewrites": rewrites,
        "anchor_lost": anchor_lost,
        "phases": phases,
        "prompt_tokens": sum(int(item["prompt_tokens"]) for item in calls),
        "completion_tokens": sum(int(item["completion_tokens"]) for item in calls),
        "total_tokens": sum(int(item["prompt_tokens"]) + int(item["completion_tokens"]) for item in calls),
        "elapsed_s": round(sum(float(item["elapsed_s"]) for item in calls), 3),
        "within_budget": all(bool(item.get("within_budget", True)) for item in calls),
        "continuation": verdict["continuation"],
    })
    print(
        f"DONE {doc_id}-F-{repeat} true={verdict['true_complete']} short={verdict['short_titles']} "
        f"lost={anchor_lost} repairs={len(repairs)} rewrites={len(rewrites)} chars={verdict['doc_chars']} "
        f"shapes={verdict['failure_shapes']}",
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
        "estimate_minus_actual": {"min": min(diffs) if diffs else 0, "max": max(diffs) if diffs else 0, "mean": round(sum(diffs) / len(diffs), 1) if diffs else 0},
        "fresh_tokens": sum(int(item["prompt_tokens"]) + int(item["completion_tokens"]) for item in fresh),
        "fresh_elapsed_s": round(sum(float(item["elapsed_s"]) for item in fresh), 1),
        "anchor_lost": sum(int(row["anchor_lost"]) for row in rows),
        "novelty_rejected": sum(1 for item in fresh if item.get("novelty_rejected")),
        "rewrites": sum(1 for item in fresh if item.get("phase") == "rewrite_current"),
        "cells": {},
    }
    cells: dict[str, object] = {}
    for key, items in sorted(grouped.items()):
        count = len(items)
        cells[key] = {
            "n": count,
            "true_complete": sum(1 for item in items if item["true_complete"]),
            "mean_doc_chars": round(sum(int(item["doc_chars"]) for item in items) / count, 1),
            "anchor_lost": sum(int(item["anchor_lost"]) for item in items),
            "rewrites": sum(len(item["rewrites"]) for item in items),
            "repairs": sum(len(item["repairs"]) for item in items),
            "failure_shapes": sorted({shape for item in items for shape in item["failure_shapes"]}),
            "guard_hits_in_doc": sum(1 for item in items if item["guard_hits"]),
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
