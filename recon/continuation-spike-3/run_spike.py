"""L0.5：D3 换三篇主题，并用质量判据拆开假完成。不进产品路径。

「完」只记录，不算完成。未完成的每一节去掉大纲要点后要有不少于 80 字的新正文，
并且不能重新写出初稿里已经出现的标题。大纲要点不到 40 字，抄一遍再加「完」过不了。
80 不是写作目标（目标仍是每节 500 字），只用来把抄要点和已经写成段落的续写分开。
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

import httpx

from spoolkit.llm.tokenizer import OfflineTokenCounter

ROOT = Path(__file__).resolve().parent
REQUESTS = ROOT / "requests.jsonl"
RUNS = ROOT / "runs.jsonl"
SUMMARY = ROOT / "summary.json"

BASE_URL = "http://127.0.0.1:8080/v1/chat/completions"
N_CTX = 8192
MARGIN = 192
MIN_OUTPUT = 128
DRAFT_CAP = 720
CONTINUE_CAP = 1500
MAX_CONTINUE_ROUNDS = 3
REPEATS = 3
TEMPERATURE = 0
SEED = 1
CEILING = N_CTX - MARGIN
MIN_NEW_BODY = 80
NGRAM = 12

COUNTER = OfflineTokenCounter()

SYSTEM = (
    "你是技术写作者。只输出 Markdown 正文。"
    "不要输出思考过程，不要调用工具，不要解释你在做什么。"
)
CONTINUE_RULE = "只输出紧接其后的新增内容，不要重复，不要重新开头，不要总结。"

DOCS: dict[str, dict[str, object]] = {
    "tech": {
        "kind": "技术说明",
        "task": (
            "请写一篇中文技术说明，题目是「本地 8K 模型怎么估算一次请求的 token」。"
            "读者是工程师。每节不少于 500 字，要有具体数字或例子。"
            "六节写完后最后一行单独写「完」。"
        ),
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
        "task": (
            "请写一篇中文项目复盘，题目是「长文续写实验为什么会假完成」。"
            "读者是一起做实验的工程师。每节不少于 500 字，要写具体现象，不要空话。"
            "六节写完后最后一行单独写「完」。"
        ),
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
        "task": (
            "请写一篇中文方案对比，题目是「长文续写、分节和整篇重写怎么选」。"
            "读者是要做实现取舍的工程师。每节不少于 500 字，每种做法都要写优点、代价和适用条件。"
            "六节写完后最后一行单独写「完」。"
        ),
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


def present_headings(text: str, doc_id: str) -> list[str]:
    return [title for title in headings_of(doc_id) if f"## {title}" in text]


def unfinished(text: str, doc_id: str) -> list[str]:
    return [title for title in headings_of(doc_id) if f"## {title}" not in text]


def last_heading(text: str, doc_id: str) -> str:
    found = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            title = stripped[3:].strip()
            if title in headings_of(doc_id):
                found = title
    return found or "（没有标题）"


def paragraph_digest(text: str) -> str:
    parts = [part.strip() for part in text.split("\n\n") if part.strip()]
    if not parts:
        return "（空）"
    last = parts[-1].replace("\n", "")
    if len(last) <= 80:
        return last
    return last[:80] + "…"


def section_bodies(text: str, doc_id: str) -> dict[str, str]:
    """取每个标题最后一次出现后的正文，直到下一个已知标题。"""
    lines = text.splitlines()
    marks: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("## "):
            title = stripped[3:].strip()
            if title in headings_of(doc_id):
                marks.append((index, title))
    bodies: dict[str, str] = {}
    for pos, (index, title) in enumerate(marks):
        end = marks[pos + 1][0] if pos + 1 < len(marks) else len(lines)
        bodies[title] = "\n".join(lines[index + 1 : end]).strip()
    return bodies


def new_body_len(body: str, outline_point: str) -> int:
    cleaned = body.replace(outline_point, "").strip()
    return len(cleaned)


def judge(draft: str, continuation: str, doc_id: str) -> dict[str, object]:
    missing = unfinished(draft, doc_id)
    bodies = section_bodies(continuation, doc_id)
    outline = outline_of(doc_id)
    per_section: list[dict[str, object]] = []
    true_sections = 0
    for title in missing:
        body = bodies.get(title, "")
        point = point_of(doc_id, title)
        fresh = new_body_len(body, point)
        copied = overlap_ratio(point, body)
        ok = title in bodies and fresh >= MIN_NEW_BODY and copied < 0.5
        if ok:
            true_sections += 1
        per_section.append(
            {
                "title": title,
                "body_chars": len(body),
                "new_body_chars": fresh,
                "outline_overlap": round(copied, 4),
                "ok": ok,
            }
        )
    merged = draft + "\n\n" + continuation
    finished = present_headings(draft, doc_id)
    reopened = [title for title in finished if f"## {title}" in continuation]
    mechanical = (
        len(present_headings(merged, doc_id)) == len(headings_of(doc_id))
        and merged.strip().endswith("完")
    )
    true_complete = bool(missing) and true_sections == len(missing) and not reopened
    return {
        "unfinished": missing,
        "reopened": reopened,
        "sections": per_section,
        "true_sections": true_sections,
        "true_complete": true_complete,
        "mechanical_complete": mechanical,
        "false_complete": mechanical and not true_complete,
        "has_ending": continuation.strip().endswith("完") or merged.strip().endswith("完"),
        "continuation_vs_draft": round(overlap_ratio(draft, continuation), 4),
        "continuation_vs_outline": round(overlap_ratio(outline, continuation), 4),
        "continuation_repeat": round(_repeat_ratio(continuation), 4),
    }


def _repeat_ratio(text: str) -> float:
    grams = ngrams(text)
    if not grams:
        return 0.0
    counts = Counter(grams)
    return sum(count for count in counts.values() if count > 1) / len(grams)


def self_check() -> None:
    doc_id = "tech"
    draft = "## 窗口\n" + ("窗口里已经写了具体数字。" * 40)
    point_dump = "\n\n".join(
        f"## {title}\n{point}" for title, point in sections_of(doc_id)
    ) + "\n\n完"
    fake = judge(draft, point_dump, doc_id)
    assert fake["mechanical_complete"] and fake["false_complete"]
    assert not fake["true_complete"]
    missing = unfinished(draft, doc_id)
    real = "\n\n".join(
        f"## {title}\n" + (f"{title}这一节要写清楚一个能操作的例子。" * 20)
        for title in missing
    ) + "\n\n完"
    good = judge(draft, real, doc_id)
    assert good["true_complete"] and not good["false_complete"]
    rewrite = "\n\n".join(
        f"## {title}\n" + ("这是把这一节重新写成长段，而不是接着未完成的位置写。" * 12)
        for title, _point in sections_of(doc_id)
    ) + "\n\n完"
    restarted = judge(draft, rewrite, doc_id)
    assert restarted["mechanical_complete"] and restarted["reopened"]
    assert not restarted["true_complete"] and restarted["false_complete"]
    assert estimate("甲" * 2000) == 2000


def draft_messages(doc_id: str) -> list[dict[str, str]]:
    user = (
        f"{DOCS[doc_id]['task']}\n\n按这个顺序写，不要先交一份只有标题的提纲：\n"
        f"{outline_of(doc_id)}\n\n从「## {headings_of(doc_id)[0]}」写起。"
    )
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user},
    ]


def strategy_messages(level: str, draft: str, doc_id: str) -> list[dict[str, str]]:
    lines = [f"大纲：\n{outline_of(doc_id)}", f"最后标题：{last_heading(draft, doc_id)}"]
    lines.append(f"最后一段摘要：{paragraph_digest(draft)}")
    if level == "D3":
        missing = "、".join(unfinished(draft, doc_id)) or "（标题都已出现）"
        lines.append(f"未完成：{missing}")
    lines.append(CONTINUE_RULE + "写完后最后一行单独写「完」。")
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "\n\n".join(lines)},
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
    extra: dict[str, object] | None = None,
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
    row: dict[str, object] = {
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
    }
    if extra:
        row.update(extra)
    append(REQUESTS, row)
    print(
        f"  tokens={actual}+{result['completion_tokens']} truncated={result['truncated']} "
        f"within={within} {result['elapsed_s']}s",
        flush=True,
    )
    return result


def load_drafts() -> dict[str, tuple[str, dict[str, object]]]:
    found: dict[str, dict[str, object]] = {}
    if not REQUESTS.exists():
        return {}
    for line in REQUESTS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if item["phase"] == "draft" and item.get("draft_ok"):
            found[str(item["doc_id"])] = item
    loaded: dict[str, tuple[str, dict[str, object]]] = {}
    for doc_id, item in found.items():
        loaded[doc_id] = (
            str(item["text"]),
            {
                "prompt_tokens": item["prompt_tokens"],
                "completion_tokens": item["completion_tokens"],
                "elapsed_s": item["elapsed_s"],
                "truncated": item["truncated"],
                "within_budget": item["within_budget"],
                "budget_blocked": False,
            },
        )
    return loaded


def draft_is_cut_in_front(text: str, truncated: bool, doc_id: str) -> bool:
    count = len(present_headings(text, doc_id))
    return bool(truncated) and 1 <= count <= 3 and len(unfinished(text, doc_id)) >= 3


def fit_draft(doc_id: str) -> tuple[str, dict[str, object]]:
    cap = DRAFT_CAP
    for _attempt in range(5):
        result = call(
            run_id=f"{doc_id}-draft",
            strategy="draft",
            doc_id=doc_id,
            repeat=1,
            phase="draft",
            round_index=0,
            messages=draft_messages(doc_id),
            cap=cap,
            extra={"draft_cap": cap},
        )
        text = str(result["text"])
        ok = draft_is_cut_in_front(text, bool(result["truncated"]), doc_id)
        _mark_last_draft(ok)
        print(
            f"{doc_id} draft cap={cap} chars={len(text)} truncated={result['truncated']} "
            f"headings={present_headings(text, doc_id)} ok={ok}",
            flush=True,
        )
        if ok:
            return text, result
        titles = present_headings(text, doc_id)
        tokens = int(result["completion_tokens"]) or cap
        if len(titles) > 3:
            fourth = headings_of(doc_id)[3]
            pos = text.find(f"## {fourth}")
            cap = max(280, int(tokens * pos / len(text) * 0.9)) if pos > 80 else max(280, int(cap * 0.65))
        elif not result["truncated"]:
            cap = min(1600, cap + 250)
        else:
            cap = min(1600, cap + 180)
    raise SystemExit(f"{doc_id} 五次都没有截在前三节")


def _mark_last_draft(ok: bool) -> None:
    lines = REQUESTS.read_text(encoding="utf-8").splitlines()
    last = json.loads(lines[-1])
    last["draft_ok"] = ok
    lines[-1] = json.dumps(last, ensure_ascii=False)
    REQUESTS.write_text("\n".join(lines) + "\n", encoding="utf-8")


def looks_repeated(previous: str, new: str) -> bool:
    head = previous[-400:]
    tail = new[:400]
    right = ngrams(tail)
    if len(right) < NGRAM:
        return False
    left = set(ngrams(head))
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
            "has_ending": bool(verdict["has_ending"]),
            "continuation_rounds": rounds,
            "repeated_rounds": repeated_rounds,
            "reopened": verdict["reopened"],
            "continuation_vs_draft": verdict["continuation_vs_draft"],
            "continuation_vs_outline": verdict["continuation_vs_outline"],
            "continuation_repeat": verdict["continuation_repeat"],
            "sections": verdict["sections"],
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "elapsed_s": round(sum(float(item["elapsed_s"]) for item in calls), 3),
            "within_budget": all(bool(item.get("within_budget", True)) for item in calls),
            "draft_headings": present_headings(draft, doc_id),
            "chars": len(draft) + len(continuation),
            "continuation": continuation,
        },
    )
    print(
        f"DONE {run_id} true={verdict['true_complete']} false={verdict['false_complete']} "
        f"outline={verdict['continuation_vs_outline']}",
        flush=True,
    )


def summarize() -> None:
    rows = [
        json.loads(line)
        for line in RUNS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    requests = [
        json.loads(line)
        for line in REQUESTS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        grouped.setdefault(f"{row['doc_id']}:{row['strategy']}", []).append(row)
    report: dict[str, object] = {
        "runs": len(rows),
        "requests": len(requests),
        "within_budget": sum(1 for item in requests if item["within_budget"]),
        "over_budget": sum(1 for item in requests if not item["within_budget"]),
        "cells": {},
    }
    for key, items in sorted(grouped.items()):
        count = len(items)
        report["cells"][key] = {
            "n": count,
            "true_complete": sum(1 for item in items if item["true_complete"]),
            "false_complete": sum(1 for item in items if item["false_complete"]),
            "mechanical_complete": sum(1 for item in items if item["mechanical_complete"]),
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
    drafts = load_drafts()
    for doc_id in DOCS:
        if doc_id not in drafts:
            drafts[doc_id] = fit_draft(doc_id)
        draft, draft_call = drafts[doc_id]
        for repeat in range(1, REPEATS + 1):
            for level in ("D2", "D3"):
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
