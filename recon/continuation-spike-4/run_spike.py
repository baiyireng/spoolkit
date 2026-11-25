"""L0.6：当前节半句没写完时，D4 会不会先把这一节收尾。不进产品路径。

D3 仍是大纲、最后标题、80 字摘要、未完成标题。
D4 在这之上加上已完成节、当前节，以及当前节最后 280 字，并要求先补完这一节。

「完」只记录。真完成还要：当前节收成完整句，每个未开始标题去掉大纲后不少于 80 字
并且自己也收成完整句，不重开已完成标题，全文不少于 3000 字。
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

import httpx

from spoolkit.llm.tokenizer import OfflineTokenCounter

ROOT = Path(__file__).resolve().parent
PRIOR = ROOT.parent / "continuation-spike-3" / "requests.jsonl"
REQUESTS = ROOT / "requests.jsonl"
RUNS = ROOT / "runs.jsonl"
SUMMARY = ROOT / "summary.json"

BASE_URL = "http://127.0.0.1:8080/v1/chat/completions"
N_CTX = 8192
MARGIN = 192
MIN_OUTPUT = 128
CONTINUE_CAP = 1500
MAX_CONTINUE_ROUNDS = 3
REPEATS = 3
TEMPERATURE = 0
SEED = 1
CEILING = N_CTX - MARGIN
MIN_NEW_BODY = 80
MIN_DOC_CHARS = 3000
CURRENT_TAIL_CHARS = 280
MIN_CURRENT_ADDED = 12
NGRAM = 12
CLOSERS = "。！？"

COUNTER = OfflineTokenCounter()

SYSTEM = (
    "你是技术写作者。只输出 Markdown 正文。"
    "不要输出思考过程，不要调用工具，不要解释你在做什么。"
)
CONTINUE_RULE = "只输出紧接其后的新增内容，不要重复，不要重新开头，不要总结。"

DOCS: dict[str, dict[str, object]] = {
    "tech": {
        "kind": "技术说明",
        "sections": (
            ("窗口", "n_ctx 是什么，一次请求里哪些部分占 token"),
            ("估算", "中文为什么不能按固定字数换算 token"),
            ("余量", "prompt 加 max_tokens 怎样才不超过窗口减余量"),
            ("截断", "输出因长度停下时，正文停在什么位置"),
            ("核对", "怎样用服务端返回的 usage 对照离线估算"),
            ("做法", "给一个工程师能照做的估算步骤"),
        ),
    },
    "retro": {
        "kind": "项目复盘",
        "sections": (
            ("目标", "原本想证明结构化续写能不能写完"),
            ("过程", "实际比较了哪些输入"),
            ("结果", "哪些次数看起来完成了"),
            ("原因", "抄大纲再加一个完字为什么能骗过检查"),
            ("改法", "完成判据应该多看哪些正文条件"),
            ("下一步", "在写实现之前还要验证什么"),
        ),
    },
    "compare": {
        "kind": "方案对比",
        "sections": (
            ("问题", "长文一次写不完时要在哪些做法里选"),
            ("续写", "带着状态接着写的优点和代价"),
            ("分节", "每节单独生成的优点和代价"),
            ("重写", "整篇推倒重来的优点和代价"),
            ("对比", "用完成、重复和 token 比较这三种做法"),
            ("建议", "什么情况下选哪一种"),
        ),
    },
}


def sections_of(doc_id: str) -> tuple[tuple[str, str], ...]:
    return DOCS[doc_id]["sections"]  # type: ignore[return-value]


def headings_of(doc_id: str) -> tuple[str, ...]:
    return tuple(title for title, _point in sections_of(doc_id))


def outline_of(doc_id: str) -> str:
    return "\n".join(f"## {title}\n{point}" for title, point in sections_of(doc_id))


def point_of(doc_id: str, title: str) -> str:
    for name, point in sections_of(doc_id):
        if name == title:
            return point
    return ""


def estimate(text: str) -> int:
    return COUNTER.count(text)


def message_tokens(messages: list[dict[str, str]]) -> int:
    return estimate("\n".join(item["content"] for item in messages))


def output_budget(messages: list[dict[str, str]], cap: int) -> tuple[int, int]:
    prompt = message_tokens(messages)
    room = CEILING - prompt
    if room < MIN_OUTPUT:
        return 0, prompt
    return min(cap, room), prompt


def ngrams(text: str) -> list[str]:
    if len(text) < NGRAM:
        return []
    return [text[index : index + NGRAM] for index in range(len(text) - NGRAM + 1)]


def overlap_ratio(left: str, right: str) -> float:
    right_grams = ngrams(right)
    if not right_grams:
        return 0.0
    left_grams = set(ngrams(left))
    return sum(1 for gram in right_grams if gram in left_grams) / len(right_grams)


def heading_marks(text: str, doc_id: str) -> list[tuple[int, str]]:
    marks: list[tuple[int, str]] = []
    for index, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if stripped.startswith("## "):
            title = stripped[3:].strip()
            if title in headings_of(doc_id):
                marks.append((index, title))
    return marks


def present_headings(text: str, doc_id: str) -> list[str]:
    found: list[str] = []
    for _index, title in heading_marks(text, doc_id):
        if title not in found:
            found.append(title)
    return found


def unfinished(text: str, doc_id: str) -> list[str]:
    seen = set(present_headings(text, doc_id))
    return [title for title in headings_of(doc_id) if title not in seen]


def last_heading(text: str, doc_id: str) -> str:
    marks = heading_marks(text, doc_id)
    return marks[-1][1] if marks else "（没有标题）"


def first_body(text: str, title: str, doc_id: str) -> str:
    lines = text.splitlines()
    marks = heading_marks(text, doc_id)
    for pos, (index, name) in enumerate(marks):
        if name != title:
            continue
        end = marks[pos + 1][0] if pos + 1 < len(marks) else len(lines)
        return "\n".join(lines[index + 1 : end]).strip()
    return ""


def last_bodies(text: str, doc_id: str) -> dict[str, str]:
    lines = text.splitlines()
    marks = heading_marks(text, doc_id)
    bodies: dict[str, str] = {}
    for pos, (index, title) in enumerate(marks):
        end = marks[pos + 1][0] if pos + 1 < len(marks) else len(lines)
        bodies[title] = "\n".join(lines[index + 1 : end]).strip()
    return bodies


def content_lines(body: str) -> list[str]:
    lines: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped == "完":
            continue
        lines.append(stripped)
    return lines


def sentence_closed(body: str) -> bool:
    lines = content_lines(body)
    return bool(lines) and lines[-1][-1] in CLOSERS


def paragraph_digest(text: str) -> str:
    parts = [part.strip() for part in text.split("\n\n") if part.strip()]
    if not parts:
        return "（空）"
    last = parts[-1].replace("\n", "")
    if len(last) <= 80:
        return last
    return last[:80] + "…"


def section_tail(body: str) -> str:
    compact = body.strip()
    if len(compact) <= CURRENT_TAIL_CHARS:
        return compact
    return compact[-CURRENT_TAIL_CHARS:]


def new_body_len(body: str, outline_point: str) -> int:
    return len(body.replace(outline_point, "").strip())


def is_mid_sentence(text: str) -> bool:
    stripped = text.rstrip()
    return bool(stripped) and stripped[-1] not in CLOSERS


def cut_mid_sentence(text: str) -> tuple[str, bool]:
    """停在句号上时，把最后一句裁短，使当前节停在半句。已经是半句则原样返回。"""
    stripped = text.rstrip()
    if is_mid_sentence(stripped):
        return stripped, False
    end = len(stripped)
    while end > 0 and stripped[end - 1] in CLOSERS + "；…」\"'）) \n":
        end -= 1
    start = max(
        stripped.rfind("。", 0, end),
        stripped.rfind("！", 0, end),
        stripped.rfind("？", 0, end),
        stripped.rfind("\n", 0, end),
    )
    sentence = stripped[start + 1 : end].strip()
    keep = max(8, int(len(sentence) * 0.55))
    if keep >= len(sentence):
        keep = max(8, len(sentence) - 4)
    clipped = sentence[:keep].rstrip("，、；： ")
    return stripped[: start + 1] + clipped, True


def _repeat_ratio(text: str) -> float:
    grams = ngrams(text)
    if not grams:
        return 0.0
    counts = Counter(grams)
    return sum(count for count in counts.values() if count > 1) / len(grams)


def judge(draft: str, continuation: str, doc_id: str) -> dict[str, object]:
    current = last_heading(draft, doc_id)
    completed = present_headings(draft, doc_id)[:-1]
    pending = unfinished(draft, doc_id)
    merged = draft + ("\n\n" + continuation if continuation else "")
    draft_body = first_body(draft, current, doc_id)
    merged_body = first_body(merged, current, doc_id)
    added = len(merged_body) - len(draft_body)
    current_closed = sentence_closed(merged_body) and added >= MIN_CURRENT_ADDED
    bodies = last_bodies(continuation, doc_id)
    per_section: list[dict[str, object]] = []
    pending_ok = 0
    for title in pending:
        body = bodies.get(title, "")
        point = point_of(doc_id, title)
        fresh = new_body_len(body, point)
        copied = overlap_ratio(point, body)
        closed = sentence_closed(body)
        ok = title in bodies and fresh >= MIN_NEW_BODY and copied < 0.5 and closed
        if ok:
            pending_ok += 1
        per_section.append(
            {
                "title": title,
                "body_chars": len(body),
                "new_body_chars": fresh,
                "outline_overlap": round(copied, 4),
                "closed": closed,
                "ok": ok,
            }
        )
    reopened = [title for title in completed if f"## {title}" in continuation]
    mechanical = (
        len(present_headings(merged, doc_id)) == len(headings_of(doc_id))
        and merged.strip().endswith("完")
    )
    long_enough = len(merged) >= MIN_DOC_CHARS
    true_complete = (
        bool(pending)
        and current_closed
        and pending_ok == len(pending)
        and not reopened
        and long_enough
    )
    return {
        "current": current,
        "current_closed": current_closed,
        "current_added_chars": added,
        "current_ending": (content_lines(merged_body) or [""])[-1][-40:],
        "pending": per_section,
        "pending_ok": pending_ok,
        "reopened": reopened,
        "doc_chars": len(merged),
        "long_enough": long_enough,
        "true_complete": true_complete,
        "mechanical_complete": mechanical,
        "false_complete": mechanical and not true_complete,
        "has_ending": merged.strip().endswith("完"),
        "continuation_vs_draft": round(overlap_ratio(draft, continuation), 4),
        "continuation_vs_outline": round(overlap_ratio(outline_of(doc_id), continuation), 4),
        "continuation_repeat": round(_repeat_ratio(continuation), 4),
    }


def self_check() -> None:
    kept, changed = cut_mid_sentence("已经写完。这句被从中间裁开成为半句。")
    assert changed and is_mid_sentence(kept) and "半句。" not in kept
    same, changed = cut_mid_sentence("总需求为")
    assert same == "总需求为" and not changed
    doc_id = "tech"
    draft = "## 窗口\n" + ("窗口这一节已经收束。" * 8) + "\n\n## 余量\n余量写到一半，总需求为"
    jump = "## 截断\n" + ("截断要写清楚停在什么位置。" * 8) + "\n\n完"
    skipped = judge(draft, jump, doc_id)
    assert not skipped["current_closed"] and not skipped["true_complete"]
    pending = unfinished(draft, doc_id)
    closed = (
        "6800，没有越过窗口。余量因此可以收束。\n\n"
        + "\n\n".join(
            f"## {title}\n" + (f"{title}这一节写出一个能核对的例子。" * 12)
            for title in pending
        )
        + "\n\n" + ("全文补足到写作目标所需的长度，用来越过三千字。" * 120)
        + "\n\n完"
    )
    good = judge(draft, closed, doc_id)
    assert good["current_closed"] and good["true_complete"] and not good["false_complete"]
    only_mark = "完"
    assert not judge(draft, only_mark, doc_id)["true_complete"]
    assert estimate("甲" * 2000) == 2000


def strategy_messages(level: str, text: str, doc_id: str) -> list[dict[str, str]]:
    if level == "D3":
        missing = "、".join(unfinished(text, doc_id)) or "（标题都已出现）"
        user = "\n\n".join(
            [
                f"大纲：\n{outline_of(doc_id)}",
                f"最后标题：{last_heading(text, doc_id)}",
                f"最后一段摘要：{paragraph_digest(text)}",
                f"未完成：{missing}",
                CONTINUE_RULE + "写完后最后一行单独写「完」。",
            ]
        )
    else:
        present = present_headings(text, doc_id)
        current = last_heading(text, doc_id)
        body = first_body(text, current, doc_id)
        if sentence_closed(body):
            completed = present
            pending = unfinished(text, doc_id)
            current_lines = ["当前节已收尾。"]
        else:
            completed = present[:-1]
            pending = unfinished(text, doc_id)
            index = headings_of(doc_id).index(current) + 1
            current_lines = [
                f"当前节：第 {index} 节「{current}」。这一节还没收尾，先补完这一节。",
                f"当前节已写：\n{section_tail(body)}",
            ]
        user = "\n\n".join(
            [
                f"大纲：\n{outline_of(doc_id)}",
                "已完成：" + ("、".join(completed) or "（还没有）"),
                *current_lines,
                "未开始：" + ("、".join(pending) or "（没有）"),
                CONTINUE_RULE + "写完后最后一行单独写「完」。",
            ]
        )
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user},
    ]


def post(messages: list[dict[str, str]], max_tokens: int) -> dict[str, object]:
    payload = {
        "model": "local",
        "temperature": TEMPERATURE,
        "seed": SEED,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
        "cache_prompt": True,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": messages,
    }
    started = time.perf_counter()
    first: float | None = None
    pieces: list[str] = []
    usage: dict[str, object] = {}
    finish = ""
    with httpx.Client(timeout=max(180.0, float(max_tokens)), trust_env=False) as client:
        with client.stream("POST", BASE_URL, json=payload) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                obj = json.loads(data)
                if obj.get("usage"):
                    usage = obj["usage"]
                for choice in obj.get("choices") or []:
                    if choice.get("finish_reason"):
                        finish = str(choice["finish_reason"])
                    piece = (choice.get("delta") or {}).get("content") or ""
                    if piece:
                        if first is None:
                            first = time.perf_counter()
                        pieces.append(piece)
    elapsed = time.perf_counter() - started
    return {
        "text": "".join(pieces),
        "finish_reason": finish or "stop",
        "truncated": finish in {"length", "max_tokens"},
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "ttft_s": round((first - started) if first is not None else elapsed, 3),
        "elapsed_s": round(elapsed, 3),
    }


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
    strategy: str,
    doc_id: str,
    repeat: int,
    phase: str,
    round_index: int,
    messages: list[dict[str, str]],
    cap: int,
) -> dict[str, object]:
    max_tokens, estimate_prompt = output_budget(messages, cap)
    print(
        f"{run_id} {phase} r{round_index} estimate={estimate_prompt} max_tokens={max_tokens}",
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
            "budget_blocked": True,
        }
    else:
        result = post(messages, max_tokens)
        result["budget_blocked"] = False
    actual = int(result["prompt_tokens"])
    within = (actual + max_tokens) <= CEILING and (estimate_prompt + max_tokens) <= CEILING
    result["max_tokens"] = max_tokens
    result["within_budget"] = within
    append(
        REQUESTS,
        {
            "run_id": run_id,
            "strategy": strategy,
            "doc_id": doc_id,
            "repeat": repeat,
            "phase": phase,
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
            "messages": messages,
            "text": result["text"],
        },
    )
    print(
        f"  tokens={actual}+{result['completion_tokens']} truncated={result['truncated']} "
        f"within={within} {result['elapsed_s']}s",
        flush=True,
    )
    return result


def load_cut_drafts() -> dict[str, tuple[str, dict[str, object]]]:
    if REQUESTS.exists():
        found: dict[str, dict[str, object]] = {}
        for line in REQUESTS.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if item["phase"] == "draft" and item.get("draft_ok"):
                found[str(item["doc_id"])] = item
        if len(found) == len(DOCS):
            return {
                doc_id: (
                    str(item["text"]),
                    {
                        "prompt_tokens": item["prompt_tokens"],
                        "completion_tokens": item["completion_tokens"],
                        "elapsed_s": item["elapsed_s"],
                        "truncated": True,
                        "within_budget": item["within_budget"],
                        "budget_blocked": False,
                    },
                )
                for doc_id, item in found.items()
            }
    prior = [
        json.loads(line)
        for line in PRIOR.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    loaded: dict[str, tuple[str, dict[str, object]]] = {}
    for doc_id in DOCS:
        source = next(
            item for item in prior if item["phase"] == "draft" and item.get("draft_ok") and item["doc_id"] == doc_id
        )
        text, changed = cut_mid_sentence(str(source["text"]))
        current = last_heading(text, doc_id)
        if not is_mid_sentence(text) or f"## {current}" not in text or not present_headings(text, doc_id)[:-1]:
            raise SystemExit(f"{doc_id} 没能裁成当前节半句：{text[-40:]}")
        print(
            f"{doc_id} cut={changed} current={current} chars={len(text)} end={text[-24:]!r}",
            flush=True,
        )
        call_meta = {
            "prompt_tokens": source["prompt_tokens"],
            "completion_tokens": source["completion_tokens"],
            "elapsed_s": source["elapsed_s"],
            "truncated": True,
            "within_budget": source["within_budget"],
            "budget_blocked": False,
        }
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
                "reused_from": "continuation-spike-3",
                "cut_mid_sentence": changed,
                "max_tokens": source["max_tokens"],
                "estimate_prompt_tokens": source["estimate_prompt_tokens"],
                "prompt_tokens": source["prompt_tokens"],
                "completion_tokens": source["completion_tokens"],
                "truncated": True,
                "finish_reason": source["finish_reason"],
                "ttft_s": source["ttft_s"],
                "elapsed_s": source["elapsed_s"],
                "estimate_minus_actual": source["estimate_minus_actual"],
                "within_budget": source["within_budget"],
                "prompt_plus_max": source["prompt_plus_max"],
                "ceiling": CEILING,
                "text": text,
            },
        )
        loaded[doc_id] = (text, call_meta)
    return loaded


def looks_repeated(previous: str, new: str) -> bool:
    right = ngrams(new[:400])
    if len(right) < NGRAM:
        return False
    left = set(ngrams(previous[-400:]))
    return sum(1 for gram in right if gram in left) / len(right) > 0.5


def continue_run(level: str, doc_id: str, repeat: int, draft: str, draft_call: dict[str, object]) -> None:
    run_id = f"{doc_id}-{level}-{repeat}"
    pieces: list[str] = []
    calls = [draft_call]
    rounds = 0
    repeated_rounds = 0
    text = draft
    while rounds < MAX_CONTINUE_ROUNDS:
        result = call(
            run_id=run_id,
            strategy=level,
            doc_id=doc_id,
            repeat=repeat,
            phase="continuation",
            round_index=rounds + 1,
            messages=strategy_messages(level, text, doc_id),
            cap=CONTINUE_CAP,
        )
        calls.append(result)
        rounds += 1
        piece = str(result["text"])
        if result.get("budget_blocked") or not piece.strip():
            break
        if looks_repeated(text, piece):
            repeated_rounds += 1
        pieces.append(piece)
        text = draft + "\n\n" + "\n\n".join(pieces)
        verdict = judge(draft, "\n\n".join(pieces), doc_id)
        if verdict["true_complete"] or verdict["mechanical_complete"]:
            break
    continuation = "\n\n".join(pieces)
    verdict = judge(draft, continuation, doc_id)
    prompt_tokens = sum(int(item["prompt_tokens"]) for item in calls)
    completion_tokens = sum(int(item["completion_tokens"]) for item in calls)
    append(
        RUNS,
        {
            "run_id": run_id,
            "strategy": level,
            "doc_id": doc_id,
            "kind": DOCS[doc_id]["kind"],
            "repeat": repeat,
            "true_complete": bool(verdict["true_complete"]),
            "false_complete": bool(verdict["false_complete"]),
            "mechanical_complete": bool(verdict["mechanical_complete"]),
            "current_closed": bool(verdict["current_closed"]),
            "current": verdict["current"],
            "current_added_chars": verdict["current_added_chars"],
            "current_ending": verdict["current_ending"],
            "has_ending": bool(verdict["has_ending"]),
            "long_enough": bool(verdict["long_enough"]),
            "doc_chars": verdict["doc_chars"],
            "continuation_rounds": rounds,
            "repeated_rounds": repeated_rounds,
            "reopened": verdict["reopened"],
            "continuation_vs_draft": verdict["continuation_vs_draft"],
            "continuation_vs_outline": verdict["continuation_vs_outline"],
            "continuation_repeat": verdict["continuation_repeat"],
            "pending": verdict["pending"],
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "elapsed_s": round(sum(float(item["elapsed_s"]) for item in calls), 3),
            "within_budget": all(bool(item.get("within_budget", True)) for item in calls),
            "continuation": continuation,
        },
    )
    print(
        f"DONE {run_id} true={verdict['true_complete']} closed={verdict['current_closed']} "
        f"chars={verdict['doc_chars']} end={verdict['current_ending']!r}",
        flush=True,
    )


def summarize() -> None:
    rows = [json.loads(line) for line in RUNS.read_text(encoding="utf-8").splitlines() if line.strip()]
    requests = [json.loads(line) for line in REQUESTS.read_text(encoding="utf-8").splitlines() if line.strip()]
    fresh = [item for item in requests if item["phase"] == "continuation"]
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        grouped.setdefault(f"{row['doc_id']}:{row['strategy']}", []).append(row)
    report: dict[str, object] = {
        "runs": len(rows),
        "requests": len(requests),
        "fresh_requests": len(fresh),
        "within_budget": sum(1 for item in requests if item["within_budget"]),
        "over_budget": sum(1 for item in requests if not item["within_budget"]),
        "fresh_tokens": sum(int(item["prompt_tokens"]) + int(item["completion_tokens"]) for item in fresh),
        "fresh_elapsed_s": round(sum(float(item["elapsed_s"]) for item in fresh), 1),
        "cells": {},
    }
    for key, items in sorted(grouped.items()):
        count = len(items)
        report["cells"][key] = {
            "n": count,
            "true_complete": sum(1 for item in items if item["true_complete"]),
            "false_complete": sum(1 for item in items if item["false_complete"]),
            "current_closed": sum(1 for item in items if item["current_closed"]),
            "long_enough": sum(1 for item in items if item["long_enough"]),
            "mean_doc_chars": round(sum(int(item["doc_chars"]) for item in items) / count, 1),
            "mean_outline_overlap": round(
                sum(float(item["continuation_vs_outline"]) for item in items) / count, 4
            ),
            "mean_draft_overlap": round(
                sum(float(item["continuation_vs_draft"]) for item in items) / count, 4
            ),
            "mean_total_tokens": round(sum(int(item["total_tokens"]) for item in items) / count, 1),
            "mean_elapsed_s": round(sum(float(item["elapsed_s"]) for item in items) / count, 1),
        }
    SUMMARY.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


def main() -> None:
    self_check()
    finished = done_ids()
    drafts = load_cut_drafts()
    for doc_id in DOCS:
        draft, draft_call = drafts[doc_id]
        for repeat in range(1, REPEATS + 1):
            for level in ("D3", "D4"):
                run_id = f"{doc_id}-{level}-{repeat}"
                if run_id in finished:
                    continue
                continue_run(level, doc_id, repeat, draft, draft_call)
    summarize()


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPError as exc:
        raise SystemExit(f"llama-server 不可用：{exc}") from exc
