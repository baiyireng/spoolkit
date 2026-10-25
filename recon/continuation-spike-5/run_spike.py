"""L0.7：当前节状态保持 D4，只改尾部怎么交给模型。不进产品路径。

A 把最后 280 字放进 assistant。B 只放最后一个完整句，最多 120 字。
C 把最后一段放进 user，并写明不要照抄。D 不交尾部，要求重写本节。
E 把最后 80 字放进 assistant，并要求从最后一个字之后继续。

「完」只能出现在大纲的最后一节。全文每节都要收成完整句，未开始节去掉大纲后不少于 80 字，
全文不少于 3000 字，续写对初稿的 12 字重叠低于 0.5。
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

import httpx

from spoolkit.llm.tokenizer import OfflineTokenCounter

ROOT = Path(__file__).resolve().parent
PRIOR = ROOT.parent / "continuation-spike-4" / "requests.jsonl"
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
MIN_CURRENT_ADDED = 12
NGRAM = 12
CLOSERS = "。！？"
LEVELS = ("A", "B", "C", "D", "E")

COUNTER = OfflineTokenCounter()
SYSTEM = (
    "你是技术写作者。只输出 Markdown 正文。"
    "不要输出思考过程，不要调用工具，不要解释你在做什么。"
)

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


def spans(text: str, doc_id: str) -> list[tuple[str, str]]:
    lines = text.splitlines()
    marks = heading_marks(text, doc_id)
    found: list[tuple[str, str]] = []
    for pos, (index, title) in enumerate(marks):
        end = marks[pos + 1][0] if pos + 1 < len(marks) else len(lines)
        found.append((title, "\n".join(lines[index + 1 : end]).strip()))
    return found


def first_body(text: str, title: str, doc_id: str) -> str:
    for name, body in spans(text, doc_id):
        if name == title:
            return body
    return ""


def last_bodies(text: str, doc_id: str) -> dict[str, str]:
    bodies: dict[str, str] = {}
    for title, body in spans(text, doc_id):
        bodies[title] = body
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


def clip_tail(body: str, limit: int) -> str:
    compact = body.strip()
    if len(compact) <= limit:
        return compact
    return compact[-limit:]


def last_complete_sentence(body: str) -> str:
    text = body.strip()
    end = max(text.rfind(mark) for mark in CLOSERS)
    if end < 0:
        return ""
    start = max(text.rfind(mark, 0, end) for mark in CLOSERS)
    start = max(start, text.rfind("\n", 0, end))
    sentence = text[start + 1 : end + 1].strip()
    if len(sentence) > 120:
        sentence = sentence[-120:]
    return sentence


def last_paragraph(body: str) -> str:
    parts = [part.strip() for part in body.split("\n\n") if part.strip()]
    return parts[-1] if parts else body.strip()


def open_section(text: str, doc_id: str) -> tuple[str | None, list[str], list[str]]:
    present = present_headings(text, doc_id)
    if not present:
        return None, [], unfinished(text, doc_id)
    current = present[-1]
    if sentence_closed(first_body(text, current, doc_id)):
        return None, present, unfinished(text, doc_id)
    return current, present[:-1], unfinished(text, doc_id)


def new_body_len(body: str, outline_point: str) -> int:
    return len(body.replace(outline_point, "").strip())


def _repeat_ratio(text: str) -> float:
    grams = ngrams(text)
    if not grams:
        return 0.0
    counts = Counter(grams)
    return sum(count for count in counts.values() if count > 1) / len(grams)


def assemble(draft: str, continuation: str, doc_id: str, level: str) -> str:
    if not continuation.strip():
        return draft
    if level != "D":
        return draft + "\n\n" + continuation
    current = present_headings(draft, doc_id)[-1]
    index = draft.find(f"## {current}")
    head = draft[:index].rstrip()
    piece = continuation.strip()
    if not piece.startswith(f"## {current}"):
        piece = f"## {current}\n\n" + piece
    return head + "\n\n" + piece


def early_done_count(text: str, doc_id: str) -> int:
    final = headings_of(doc_id)[-1]
    lines = text.splitlines()
    cut = len(lines)
    for index, line in enumerate(lines):
        if line.strip() == f"## {final}":
            cut = index
            break
    return sum(1 for line in lines[:cut] if line.strip() == "完")


def judge(draft: str, continuation: str, doc_id: str, level: str) -> dict[str, object]:
    current = present_headings(draft, doc_id)[-1]
    pending = unfinished(draft, doc_id)
    merged = assemble(draft, continuation, doc_id, level)
    draft_body = first_body(draft, current, doc_id)
    merged_body = first_body(merged, current, doc_id)
    added = len(merged_body) - len(draft_body)
    if level == "D":
        current_closed = sentence_closed(merged_body) and new_body_len(merged_body, point_of(doc_id, current)) >= MIN_NEW_BODY
    else:
        current_closed = sentence_closed(merged_body) and added >= MIN_CURRENT_ADDED
    open_sections = [
        title for title, body in spans(merged, doc_id) if not sentence_closed(body)
    ]
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
                "new_body_chars": fresh,
                "outline_overlap": round(copied, 4),
                "closed": closed,
                "ok": ok,
            }
        )
    draft_overlap = round(overlap_ratio(draft, continuation), 4)
    early = early_done_count(merged, doc_id)
    long_enough = len(merged) >= MIN_DOC_CHARS
    true_complete = (
        bool(pending)
        and current_closed
        and not open_sections
        and early == 0
        and pending_ok == len(pending)
        and long_enough
        and draft_overlap < 0.5
    )
    mechanical = (
        len(present_headings(merged, doc_id)) == len(headings_of(doc_id))
        and merged.strip().endswith("完")
    )
    return {
        "current": current,
        "current_closed": current_closed,
        "current_added_chars": added,
        "current_ending": (content_lines(merged_body) or [""])[-1][-40:],
        "open_sections": open_sections,
        "half_residuals": len(open_sections),
        "early_done": early,
        "pending": per_section,
        "pending_ok": pending_ok,
        "doc_chars": len(merged),
        "long_enough": long_enough,
        "draft_overlap": draft_overlap,
        "outline_overlap": round(overlap_ratio(outline_of(doc_id), continuation), 4),
        "continuation_repeat": round(_repeat_ratio(continuation), 4),
        "true_complete": true_complete,
        "mechanical_complete": mechanical,
        "false_complete": mechanical and not true_complete,
        "has_ending": merged.strip().endswith("完"),
    }


def self_check() -> None:
    assert last_complete_sentence("前一句完整。半句还没") == "前一句完整。"
    assert len(clip_tail("甲" * 300, 280)) == 280
    assert len(clip_tail("甲" * 100, 80)) == 80
    doc_id = "tech"
    draft = "## 窗口\n窗口这一节已经写完。\n\n## 余量\n余量写到一半，总需求为"
    pending = unfinished(draft, doc_id)
    good_text = (
        "6800，没有越过窗口。余量因此收成完整段落。\n\n"
        + "\n\n".join(
            f"## {title}\n" + (f"{title}这一节写出一个可以核对的例子。" * 12)
            for title in pending
        )
        + "\n\n" + ("补足全文长度，让六节越过三千字的下限。" * 120)
        + "\n\n完"
    )
    good = judge(draft, good_text, doc_id, "A")
    assert good["current_closed"] and good["true_complete"] and good["early_done"] == 0
    early = good_text.replace("## 截断", "完\n\n## 截断", 1)
    assert judge(draft, early, doc_id, "A")["early_done"] >= 1
    assert not judge(draft, early, doc_id, "A")["true_complete"]
    replay = (draft[-180:] + "\n") * 3
    assert judge(draft, replay, doc_id, "A")["draft_overlap"] >= 0.5
    assert not judge(draft, replay, doc_id, "A")["true_complete"]
    replaced = assemble(draft, "## 余量\n这一节整段重写完毕。", doc_id, "D")
    assert "总需求为" not in replaced and "整段重写完毕。" in replaced
    assert estimate("甲" * 2000) == 2000


def follow_text(level: str, closed: bool) -> str:
    if closed:
        return (
            "当前节已收尾。只写未开始的标题，每节收成完整段落。"
            "不要重复，不要在中间写「完」。全文最后一行单独写「完」。"
        )
    if level == "D":
        return (
            "不要接半句。请重写完整的当前节，收成完整段落。"
            "写完后再写未开始的标题。不要重复已完成的节。"
            "不要在中间写「完」。全文最后一行单独写「完」。"
        )
    if level == "E":
        return (
            "只从最后一个字之后继续。不要重复前面的字，不要重新开头。"
            "先补完当前节，再写未开始的标题。"
            "不要在中间写「完」。全文最后一行单独写「完」。"
        )
    if level == "C":
        return (
            "不要照抄上面的结尾。先把当前节补成完整段落，再写未开始的标题。"
            "只输出新增内容。不要在中间写「完」。全文最后一行单独写「完」。"
        )
    return (
        "只输出紧接其后的新增内容，不要重复，不要重新开头，不要总结。"
        "先补完当前节，再写未开始的标题。"
        "不要在中间写「完」。全文最后一行单独写「完」。"
    )


def strategy_messages(level: str, text: str, doc_id: str) -> list[dict[str, str]]:
    current, completed, pending = open_section(text, doc_id)
    lines = [
        f"大纲：\n{outline_of(doc_id)}",
        "已完成：" + ("、".join(completed) or "（还没有）"),
    ]
    tail = ""
    if current is None:
        lines.append("当前节已收尾。")
        lines.append("未开始：" + ("、".join(pending) or "（没有）"))
    else:
        index = headings_of(doc_id).index(current) + 1
        body = first_body(text, current, doc_id)
        lines.append(f"当前节：第 {index} 节「{current}」。这一节还没收尾，先补完这一节。")
        if level == "C":
            lines.append("以下只是已写结尾，仅作定位，不要照抄：\n" + last_paragraph(body))
        if level == "D":
            previous = completed[-1] if completed else ""
            summary = paragraph_digest(first_body(text, previous, doc_id)) if previous else "（没有上一节）"
            lines.append(f"要点：{point_of(doc_id, current)}")
            lines.append(f"上一节摘要：{summary}")
        lines.append("未开始：" + ("、".join(pending) or "（没有）"))
        if level == "A":
            tail = clip_tail(body, 280)
        elif level == "B":
            tail = last_complete_sentence(body)
        elif level == "E":
            tail = clip_tail(body, 80)
    follow = follow_text(level, current is None)
    if tail:
        return [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": "\n\n".join(lines)},
            {"role": "assistant", "content": tail},
            {"role": "user", "content": follow},
        ]
    lines.append(follow)
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
    round_index: int,
    messages: list[dict[str, str]],
) -> dict[str, object]:
    max_tokens, estimate_prompt = output_budget(messages, CONTINUE_CAP)
    print(f"{run_id} r{round_index} estimate={estimate_prompt} max_tokens={max_tokens}", flush=True)
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
            "phase": "continuation",
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


def load_drafts() -> dict[str, str]:
    if REQUESTS.exists():
        found: dict[str, str] = {}
        for line in REQUESTS.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if item.get("phase") == "draft" and item.get("draft_ok"):
                found[str(item["doc_id"])] = str(item["text"])
        if len(found) == len(DOCS):
            return found
    prior = [
        json.loads(line)
        for line in PRIOR.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    loaded: dict[str, str] = {}
    for doc_id in DOCS:
        source = next(
            item
            for item in prior
            if item.get("phase") == "draft" and item.get("draft_ok") and item["doc_id"] == doc_id
        )
        text = str(source["text"]).rstrip()
        current = present_headings(text, doc_id)[-1]
        if sentence_closed(first_body(text, current, doc_id)):
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
                "reused_from": "continuation-spike-4",
                "text": text,
                "within_budget": True,
                "prompt_tokens": source.get("prompt_tokens", 0),
                "completion_tokens": source.get("completion_tokens", 0),
                "elapsed_s": 0,
            },
        )
        loaded[doc_id] = text
    return loaded


def looks_repeated(previous: str, new: str) -> bool:
    right = ngrams(new[:400])
    if len(right) < NGRAM:
        return False
    left = set(ngrams(previous[-400:]))
    return sum(1 for gram in right if gram in left) / len(right) > 0.5


def continue_run(level: str, doc_id: str, repeat: int, draft: str) -> None:
    run_id = f"{doc_id}-{level}-{repeat}"
    pieces: list[str] = []
    calls: list[dict[str, object]] = []
    rounds = 0
    repeated_rounds = 0
    text = draft
    while rounds < MAX_CONTINUE_ROUNDS:
        result = call(
            run_id=run_id,
            strategy=level,
            doc_id=doc_id,
            repeat=repeat,
            round_index=rounds + 1,
            messages=strategy_messages(level, text, doc_id),
        )
        calls.append(result)
        rounds += 1
        piece = str(result["text"])
        if result.get("budget_blocked") or not piece.strip():
            break
        if looks_repeated(text, piece):
            repeated_rounds += 1
        pieces.append(piece)
        continuation = "\n\n".join(pieces)
        text = assemble(draft, continuation, doc_id, level)
        verdict = judge(draft, continuation, doc_id, level)
        if verdict["true_complete"] or verdict["mechanical_complete"]:
            break
    continuation = "\n\n".join(pieces)
    verdict = judge(draft, continuation, doc_id, level)
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
            "current_ending": verdict["current_ending"],
            "half_residuals": verdict["half_residuals"],
            "open_sections": verdict["open_sections"],
            "early_done": verdict["early_done"],
            "doc_chars": verdict["doc_chars"],
            "long_enough": bool(verdict["long_enough"]),
            "draft_overlap": verdict["draft_overlap"],
            "outline_overlap": verdict["outline_overlap"],
            "continuation_repeat": verdict["continuation_repeat"],
            "pending": verdict["pending"],
            "continuation_rounds": rounds,
            "repeated_rounds": repeated_rounds,
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
        f"half={verdict['half_residuals']} early={verdict['early_done']} "
        f"chars={verdict['doc_chars']} overlap={verdict['draft_overlap']}",
        flush=True,
    )


def summarize() -> None:
    rows = [json.loads(line) for line in RUNS.read_text(encoding="utf-8").splitlines() if line.strip()]
    requests = [json.loads(line) for line in REQUESTS.read_text(encoding="utf-8").splitlines() if line.strip()]
    fresh = [item for item in requests if item.get("phase") == "continuation"]
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        grouped.setdefault(f"{row['doc_id']}:{row['strategy']}", []).append(row)
    report: dict[str, object] = {
        "runs": len(rows),
        "fresh_requests": len(fresh),
        "within_budget": sum(1 for item in fresh if item["within_budget"]),
        "over_budget": sum(1 for item in fresh if not item["within_budget"]),
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
            "mean_doc_chars": round(sum(int(item["doc_chars"]) for item in items) / count, 1),
            "mean_draft_overlap": round(sum(float(item["draft_overlap"]) for item in items) / count, 4),
            "mean_half_residuals": round(sum(int(item["half_residuals"]) for item in items) / count, 2),
            "early_done": sum(int(item["early_done"]) for item in items),
            "repeated_rounds": sum(int(item["repeated_rounds"]) for item in items),
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
        draft = drafts[doc_id]
        for level in LEVELS:
            for repeat in range(1, REPEATS + 1):
                run_id = f"{doc_id}-{level}-{repeat}"
                if run_id in finished:
                    continue
                continue_run(level, doc_id, repeat, draft)
    summarize()


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPError as exc:
        raise SystemExit(f"llama-server 不可用：{exc}") from exc
