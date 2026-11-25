"""L0.9：F 形状加上目标字数和输出守卫。不进产品路径。

E 仍是 L0.8 的单次接法，申请上限 1500，用来对照。
F 先只补当前节，再用一次请求按每节 500 字写完全部未开始标题，全文目标 3000 字。
各节都完整、并且返回里没有思考或提前的「完」，才允许最后一行写「完」。
守卫只判失败，不把思考从正文里删掉。
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path

import httpx

from spoolkit.llm.tokenizer import OfflineTokenCounter

ROOT = Path(__file__).resolve().parent
PRIOR = ROOT.parent / "continuation-spike-6" / "requests.jsonl"
REQUESTS = ROOT / "requests.jsonl"
RUNS = ROOT / "runs.jsonl"
SUMMARY = ROOT / "summary.json"

BASE_URL = "http://127.0.0.1:8080/v1/chat/completions"
N_CTX = 8192
MARGIN = 192
MIN_OUTPUT = 128
E_CAP = 1500
FINISH_CAP = 1500
MAX_E_ROUNDS = 3
MAX_FINISH_ROUNDS = 2
REPEATS = 3
TEMPERATURE = 0
SEED = 1
CEILING = N_CTX - MARGIN
TARGET_CHARS = 500
LENGTH_FLOOR = 3000
SECTION_FLOOR = TARGET_CHARS - TARGET_CHARS // 10
LEGACY_NEW_BODY = 80
MIN_CURRENT_ADDED = 12
NGRAM = 12
CLOSERS = "。！？"
LEVELS = ("E", "F")
ENGLISH_LETTERS = 160
ENGLISH_RATIO = 0.55

COUNTER = OfflineTokenCounter()
SYSTEM = (
    "你是技术写作者。只输出 Markdown 正文。"
    "不要输出思考过程，不要调用工具，不要解释你在做什么。"
)
THINK_TAG = re.compile(r"</?think>", re.IGNORECASE)
META_TERM = re.compile(r"\b(thinking|let me|analysis)\b", re.IGNORECASE)

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


def pending_output_cap(n_titles: int) -> int:
    return n_titles * TARGET_CHARS * 2 + 400


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


def new_body_len(body: str, outline_point: str) -> int:
    return len(body.replace(outline_point, "").strip())


def open_section(text: str, doc_id: str) -> tuple[str | None, list[str], list[str]]:
    present = present_headings(text, doc_id)
    if not present:
        return None, [], unfinished(text, doc_id)
    current = present[-1]
    if sentence_closed(first_body(text, current, doc_id)):
        return None, present, unfinished(text, doc_id)
    return current, present[:-1], unfinished(text, doc_id)


def _repeat_ratio(text: str) -> float:
    grams = ngrams(text)
    if not grams:
        return 0.0
    counts = Counter(grams)
    return sum(count for count in counts.values() if count > 1) / len(grams)


def early_done_count(text: str, doc_id: str) -> int:
    final = headings_of(doc_id)[-1]
    lines = text.splitlines()
    cut = len(lines)
    for index, line in enumerate(lines):
        if line.strip() == f"## {final}":
            cut = index
            break
    return sum(1 for line in lines[:cut] if line.strip() == "完")


def output_guard(text: str) -> dict[str, object]:
    hits: list[str] = []
    if THINK_TAG.search(text):
        hits.append("think_tag")
    terms = sorted({match.group(0).lower() for match in META_TERM.finditer(text)})
    if terms:
        hits.append("meta")
    english: list[str] = []
    for part in re.split(r"\n\s*\n", text):
        letters = re.findall(r"[A-Za-z]", part)
        compact = re.sub(r"\s+", "", part)
        if len(letters) >= ENGLISH_LETTERS and compact and len(letters) / len(compact) >= ENGLISH_RATIO:
            english.append(part.strip())
    if english:
        hits.append("english_analysis")
    return {
        "guard_hits": hits,
        "meta_terms": terms,
        "english_paragraphs": len(english),
        "english_chars": sum(len(part) for part in english),
    }


def judge(draft: str, continuation: str, doc_id: str) -> dict[str, object]:
    current = present_headings(draft, doc_id)[-1]
    pending = unfinished(draft, doc_id)
    merged = draft + ("\n\n" + continuation if continuation else "")
    draft_body = first_body(draft, current, doc_id)
    merged_body = first_body(merged, current, doc_id)
    added = len(merged_body) - len(draft_body)
    current_closed = sentence_closed(merged_body) and added >= MIN_CURRENT_ADDED
    fragment = draft.rstrip().splitlines()[-1].strip()
    fragment_remains = bool(fragment) and fragment in merged and not fragment.endswith(tuple(CLOSERS))
    open_sections = [title for title, body in spans(merged, doc_id) if not sentence_closed(body)]
    bodies = last_bodies(continuation, doc_id)
    merged_bodies = last_bodies(merged, doc_id)
    per_section: list[dict[str, object]] = []
    pending_ok = 0
    legacy_ok_n = 0
    for title in pending:
        body = bodies.get(title) or merged_bodies.get(title, "")
        point = point_of(doc_id, title)
        fresh = new_body_len(body, point)
        copied = overlap_ratio(point, body) if body else 0.0
        closed = sentence_closed(body)
        body_chars = len(body.strip())
        at_target = body_chars >= SECTION_FLOOR and fresh > 0 and copied < 0.5 and closed
        if at_target:
            pending_ok += 1
        if fresh >= LEGACY_NEW_BODY and copied < 0.5 and closed:
            legacy_ok_n += 1
        per_section.append(
            {
                "title": title,
                "target_chars": TARGET_CHARS,
                "body_chars": body_chars,
                "new_body_chars": fresh,
                "outline_overlap": round(copied, 4),
                "closed": closed,
                "at_target": at_target,
            }
        )
    guard = output_guard(continuation)
    draft_overlap = round(overlap_ratio(draft, continuation), 4)
    outline_overlap = round(overlap_ratio(outline_of(doc_id), continuation), 4)
    early = early_done_count(merged, doc_id)
    long_enough = len(merged) >= LENGTH_FLOOR
    shapes: list[str] = []
    if not current_closed:
        shapes.append("current_open")
    if open_sections:
        shapes.append("open_section")
    if early:
        shapes.append("early_done")
    shapes.extend(str(hit) for hit in guard["guard_hits"])
    if any(not item["at_target"] for item in per_section):
        shapes.append("short_section")
    if not long_enough:
        shapes.append("short_doc")
    if any(float(item["outline_overlap"]) >= 0.5 for item in per_section) or outline_overlap >= 0.5:
        shapes.append("outline_overlap")
    if draft_overlap >= 0.5:
        shapes.append("draft_overlap")
    true_complete = bool(pending) and not shapes
    legacy_complete = (
        bool(pending)
        and current_closed
        and not open_sections
        and early == 0
        and legacy_ok_n == len(pending)
        and long_enough
        and draft_overlap < 0.5
    )
    return {
        "current": current,
        "current_closed": current_closed,
        "current_ending": (content_lines(merged_body) or [""])[-1][-40:],
        "fragment_remains": fragment_remains,
        "open_sections": open_sections,
        "half_residuals": len(open_sections),
        "early_done": early,
        "pending": per_section,
        "pending_ok": pending_ok,
        "doc_chars": len(merged),
        "long_enough": long_enough,
        "draft_overlap": draft_overlap,
        "outline_overlap": outline_overlap,
        "continuation_repeat": round(_repeat_ratio(continuation), 4),
        "guard_hits": guard["guard_hits"],
        "meta_terms": guard["meta_terms"],
        "english_chars": guard["english_chars"],
        "failure_shapes": shapes,
        "true_complete": true_complete,
        "legacy_complete": legacy_complete,
        "has_ending": merged.strip().endswith("完"),
    }


def self_check() -> None:
    doc_id = "tech"
    draft = "## 窗口\n窗口这一节已经写完。\n\n## 余量\n余量写到一半，总需求为"
    pending = unfinished(draft, doc_id)
    assert pending == ["估算", "截断", "核对", "做法"]
    assert SECTION_FLOOR == 450
    assert "80" not in pending_batch_messages(draft, doc_id, pending)[-1]["content"]
    good_text = (
        "6800，没有越过窗口。余量因此把 max_tokens 和 n_ctx 留在预算里。\n\n"
        + "\n\n".join(f"## {title}\n" + (f"{title}这一节写出可以核对的做法。" * 50) for title in pending)
    )
    good = judge(draft, good_text, doc_id)
    assert good["current_closed"] and good["true_complete"] and good["guard_hits"] == []
    assert good["long_enough"] and good["pending_ok"] == len(pending)
    leaked = (
        "<think>\nThe user wants an analysis. Let me look at the draft before writing.\n\n"
        + ("This paragraph keeps analyzing the previous section in English. " * 8)
        + "\n</think>\n\n"
        + good_text
    )
    leaked_verdict = judge(draft, leaked, doc_id)
    assert not leaked_verdict["true_complete"]
    assert set(leaked_verdict["guard_hits"]) >= {"think_tag", "meta", "english_analysis"}
    assert "<think>" in leaked and leaked_verdict["doc_chars"] > good["doc_chars"]
    early = "6800，没有越过窗口。余量因此收成完整段落。\n\n完\n\n" + "\n\n".join(
        f"## {title}\n" + (f"{title}这一节写出可以核对的做法。" * 40) for title in pending
    )
    early_verdict = judge(draft, early, doc_id)
    assert early_verdict["early_done"] >= 1 and not early_verdict["true_complete"]
    assert "early_done" in early_verdict["failure_shapes"]
    short = "6800，没有越过窗口。余量因此收成完整段落。\n\n" + "\n\n".join(
        f"## {title}\n{title}这一节只有一小段，还没有写到目标。" for title in pending
    )
    short_verdict = judge(draft, short + ("补字。" * 400), doc_id)
    assert not short_verdict["true_complete"] and "short_section" in short_verdict["failure_shapes"]
    plain = "把 max_tokens 和 n_ctx 放在同一段里，对照 prompt_tokens。"
    assert output_guard(plain)["guard_hits"] == []
    assert estimate("甲" * 2000) == 2000


def e_messages(text: str, doc_id: str) -> list[dict[str, str]]:
    current, completed, pending = open_section(text, doc_id)
    lines = [
        f"大纲：\n{outline_of(doc_id)}",
        "已完成：" + ("、".join(completed) or "（还没有）"),
    ]
    tail = ""
    if current is None:
        lines.append("当前节已收尾。")
        lines.append("未开始：" + ("、".join(pending) or "（没有）"))
        follow = (
            "当前节已收尾。只写未开始的标题，每节收成完整段落。"
            "不要重复，不要在中间写「完」。全文最后一行单独写「完」。"
        )
    else:
        index = headings_of(doc_id).index(current) + 1
        body = first_body(text, current, doc_id)
        lines.append(f"当前节：第 {index} 节「{current}」。这一节还没收尾，先补完这一节。")
        lines.append("未开始：" + ("、".join(pending) or "（没有）"))
        tail = clip_tail(body, 80)
        follow = (
            "只从最后一个字之后继续。不要重复前面的字，不要重新开头。"
            "先补完当前节，再写未开始的标题。"
            "不要在中间写「完」。全文最后一行单独写「完」。"
        )
    if tail:
        return [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": "\n\n".join(lines)},
            {"role": "assistant", "content": tail},
            {"role": "user", "content": follow},
        ]
    lines.append(follow)
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "\n\n".join(lines)}]


def finish_messages(text: str, doc_id: str) -> list[dict[str, str]]:
    current, _completed, _pending = open_section(text, doc_id)
    if current is None:
        current = present_headings(text, doc_id)[-1]
    index = headings_of(doc_id).index(current) + 1
    body = first_body(text, current, doc_id)
    user = f"当前节：第 {index} 节「{current}」。只补完这一节，不要写其他标题，不要写「完」。"
    follow = "只从最后一个字之后继续，把这一节收成完整段落。不要写其他节，不要写「完」。"
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user},
        {"role": "assistant", "content": clip_tail(body, 80)},
        {"role": "user", "content": follow},
    ]


def completed_summaries(text: str, doc_id: str) -> str:
    lines: list[str] = []
    for title in present_headings(text, doc_id):
        body = first_body(text, title, doc_id)
        if sentence_closed(body):
            lines.append(f"- {title}：{paragraph_digest(body)}")
    return "\n".join(lines) or "（还没有）"


def pending_batch_messages(text: str, doc_id: str, titles: list[str]) -> list[dict[str, str]]:
    targets = "\n".join(
        f"- ## {title}：目标 {TARGET_CHARS} 字。要点：{point_of(doc_id, title)}" for title in titles
    )
    user = "\n\n".join(
        [
            f"大纲：\n{outline_of(doc_id)}",
            "已完成节摘要：\n" + completed_summaries(text, doc_id),
            "未开始标题，每节带目标字数：\n" + targets,
            (
                f"全文目标不少于 {LENGTH_FLOOR} 字。"
                f"上面每一节都写到自己的 {TARGET_CHARS} 字，收成完整段落。"
                "逐个标题写新正文。"
                "不要写「完」。不要把大纲要点原样抄一遍。不要重复已完成的节。"
            ),
        ]
    )
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def finalize_messages() -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "各节都已写完。只输出最后一行「完」。不要写其他内容。"},
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
    print(f"{run_id} {phase} r{round_index} estimate={estimate_prompt} max_tokens={max_tokens}", flush=True)
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
    guard = output_guard(str(result["text"]))
    result["max_tokens"] = max_tokens
    result["within_budget"] = within
    result["guard_hits"] = guard["guard_hits"]
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
            "guard_hits": guard["guard_hits"],
            "meta_terms": guard["meta_terms"],
            "english_chars": guard["english_chars"],
            "messages": messages,
            "text": result["text"],
        },
    )
    print(
        f"  tokens={actual}+{result['completion_tokens']} truncated={result['truncated']} "
        f"within={within} guard={guard['guard_hits'] or '-'} {result['elapsed_s']}s",
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
    prior = [json.loads(line) for line in PRIOR.read_text(encoding="utf-8").splitlines() if line.strip()]
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
                "reused_from": "continuation-spike-6",
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


def store_run(
    *,
    level: str,
    doc_id: str,
    repeat: int,
    draft: str,
    pieces: list[str],
    calls: list[dict[str, object]],
    repeated_rounds: int,
    phases: dict[str, int],
) -> None:
    continuation = "\n\n".join(pieces)
    verdict = judge(draft, continuation, doc_id)
    prompt_tokens = sum(int(item["prompt_tokens"]) for item in calls)
    completion_tokens = sum(int(item["completion_tokens"]) for item in calls)
    append(
        RUNS,
        {
            "run_id": f"{doc_id}-{level}-{repeat}",
            "strategy": level,
            "doc_id": doc_id,
            "kind": DOCS[doc_id]["kind"],
            "repeat": repeat,
            "true_complete": bool(verdict["true_complete"]),
            "legacy_complete": bool(verdict["legacy_complete"]),
            "current_closed": bool(verdict["current_closed"]),
            "current_ending": verdict["current_ending"],
            "fragment_remains": bool(verdict["fragment_remains"]),
            "half_residuals": verdict["half_residuals"],
            "open_sections": verdict["open_sections"],
            "early_done": verdict["early_done"],
            "pending_ok": verdict["pending_ok"],
            "pending": verdict["pending"],
            "doc_chars": verdict["doc_chars"],
            "long_enough": bool(verdict["long_enough"]),
            "draft_overlap": verdict["draft_overlap"],
            "outline_overlap": verdict["outline_overlap"],
            "continuation_repeat": verdict["continuation_repeat"],
            "guard_hits": verdict["guard_hits"],
            "meta_terms": verdict["meta_terms"],
            "english_chars": verdict["english_chars"],
            "failure_shapes": verdict["failure_shapes"],
            "has_ending": bool(verdict["has_ending"]),
            "phases": phases,
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
        f"DONE {doc_id}-{level}-{repeat} true={verdict['true_complete']} "
        f"closed={verdict['current_closed']} pending={verdict['pending_ok']} "
        f"early={verdict['early_done']} chars={verdict['doc_chars']} "
        f"guard={verdict['guard_hits'] or '-'} shapes={verdict['failure_shapes']}",
        flush=True,
    )


def accept_piece(text: str, piece: str, pieces: list[str]) -> int:
    repeated = 1 if piece.strip() and looks_repeated(text, piece) else 0
    if piece.strip():
        pieces.append(piece)
    return repeated


def run_e(doc_id: str, repeat: int, draft: str) -> None:
    run_id = f"{doc_id}-E-{repeat}"
    pieces: list[str] = []
    calls: list[dict[str, object]] = []
    repeated = 0
    text = draft
    rounds = 0
    while rounds < MAX_E_ROUNDS:
        result = call(
            run_id=run_id,
            strategy="E",
            doc_id=doc_id,
            repeat=repeat,
            phase="e",
            round_index=rounds + 1,
            messages=e_messages(text, doc_id),
            cap=E_CAP,
        )
        calls.append(result)
        rounds += 1
        piece = str(result["text"])
        if result.get("budget_blocked") or not piece.strip():
            break
        repeated += accept_piece(text, piece, pieces)
        text = draft + "\n\n" + "\n\n".join(pieces)
        verdict = judge(draft, "\n\n".join(pieces), doc_id)
        if verdict["legacy_complete"] or (
            len(present_headings(text, doc_id)) == len(headings_of(doc_id)) and text.strip().endswith("完")
        ):
            break
    store_run(
        level="E",
        doc_id=doc_id,
        repeat=repeat,
        draft=draft,
        pieces=pieces,
        calls=calls,
        repeated_rounds=repeated,
        phases={"e": rounds},
    )


def section_at_target(text: str, title: str, doc_id: str) -> bool:
    body = last_bodies(text, doc_id).get(title, "")
    if not body:
        return False
    point = point_of(doc_id, title)
    return (
        len(body.strip()) >= SECTION_FLOOR
        and new_body_len(body, point) > 0
        and sentence_closed(body)
        and overlap_ratio(point, body) < 0.5
    )


def run_f(doc_id: str, repeat: int, draft: str) -> None:
    run_id = f"{doc_id}-F-{repeat}"
    pieces: list[str] = []
    calls: list[dict[str, object]] = []
    repeated = 0
    text = draft
    finish_rounds = 0
    pending_calls = 0
    finalized = 0
    original_pending = unfinished(draft, doc_id)
    current_title = present_headings(draft, doc_id)[-1]
    for _attempt in range(MAX_FINISH_ROUNDS):
        if open_section(text, doc_id)[0] is None:
            break
        result = call(
            run_id=run_id,
            strategy="F",
            doc_id=doc_id,
            repeat=repeat,
            phase="finish_current",
            round_index=finish_rounds + 1,
            messages=finish_messages(text, doc_id),
            cap=FINISH_CAP,
        )
        calls.append(result)
        finish_rounds += 1
        piece = str(result["text"])
        if result.get("budget_blocked") or not piece.strip():
            break
        repeated += accept_piece(text, piece, pieces)
        text = draft + "\n\n" + "\n\n".join(pieces)
        if sentence_closed(first_body(text, current_title, doc_id)):
            break
    missing = [title for title in original_pending if not section_at_target(text, title, doc_id)]
    if missing:
        result = call(
            run_id=run_id,
            strategy="F",
            doc_id=doc_id,
            repeat=repeat,
            phase="write_pending",
            round_index=1,
            messages=pending_batch_messages(text, doc_id, missing),
            cap=pending_output_cap(len(missing)),
        )
        calls.append(result)
        pending_calls = 1
        piece = str(result["text"])
        if piece.strip() and not result.get("budget_blocked"):
            repeated += accept_piece(text, piece, pieces)
            text = draft + "\n\n" + "\n\n".join(pieces)
    verdict = judge(draft, "\n\n".join(pieces), doc_id)
    ready = (
        verdict["current_closed"]
        and verdict["pending_ok"] == len(original_pending)
        and verdict["early_done"] == 0
        and not verdict["open_sections"]
        and not verdict["guard_hits"]
        and verdict["long_enough"]
        and not verdict["has_ending"]
    )
    if ready:
        result = call(
            run_id=run_id,
            strategy="F",
            doc_id=doc_id,
            repeat=repeat,
            phase="finalize",
            round_index=1,
            messages=finalize_messages(),
            cap=64,
        )
        calls.append(result)
        finalized = 1
        piece = str(result["text"]).strip()
        if piece and not result.get("budget_blocked"):
            pieces.append(piece)
    store_run(
        level="F",
        doc_id=doc_id,
        repeat=repeat,
        draft=draft,
        pieces=pieces,
        calls=calls,
        repeated_rounds=repeated,
        phases={"finish_current": finish_rounds, "write_pending": pending_calls, "finalize": finalized},
    )


def summarize() -> None:
    rows = [json.loads(line) for line in RUNS.read_text(encoding="utf-8").splitlines() if line.strip()]
    requests = [json.loads(line) for line in REQUESTS.read_text(encoding="utf-8").splitlines() if line.strip()]
    fresh = [item for item in requests if item.get("phase") != "draft"]
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        grouped.setdefault(f"{row['doc_id']}:{row['strategy']}", []).append(row)
    diffs = [int(item["estimate_minus_actual"]) for item in fresh]
    report: dict[str, object] = {
        "runs": len(rows),
        "fresh_requests": len(fresh),
        "within_budget": sum(1 for item in fresh if item["within_budget"]),
        "over_budget": sum(1 for item in fresh if not item["within_budget"]),
        "max_prompt_plus_max": max(int(item["prompt_plus_max"]) for item in fresh),
        "estimate_minus_actual": {
            "min": min(diffs),
            "max": max(diffs),
            "mean": round(sum(diffs) / len(diffs), 1),
        },
        "fresh_tokens": sum(int(item["prompt_tokens"]) + int(item["completion_tokens"]) for item in fresh),
        "fresh_elapsed_s": round(sum(float(item["elapsed_s"]) for item in fresh), 1),
        "guard_requests": sum(1 for item in fresh if item.get("guard_hits")),
        "target_chars": TARGET_CHARS,
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
            "legacy_complete": sum(1 for item in items if item["legacy_complete"]),
            "current_closed": sum(1 for item in items if item["current_closed"]),
            "mean_pending_ok": round(sum(int(item["pending_ok"]) for item in items) / count, 2),
            "mean_doc_chars": round(sum(int(item["doc_chars"]) for item in items) / count, 1),
            "long_enough": sum(1 for item in items if item["long_enough"]),
            "mean_draft_overlap": round(sum(float(item["draft_overlap"]) for item in items) / count, 4),
            "mean_outline_overlap": round(sum(float(item["outline_overlap"]) for item in items) / count, 4),
            "early_done": sum(int(item["early_done"]) for item in items),
            "guard_runs": sum(1 for item in items if item["guard_hits"]),
            "english_chars": sum(int(item["english_chars"]) for item in items),
            "failure_shapes": sorted({shape for item in items for shape in item["failure_shapes"]}),
            "fragment_remains": sum(1 for item in items if item["fragment_remains"]),
            "repeated_rounds": sum(int(item["repeated_rounds"]) for item in items),
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
    for doc_id in DOCS:
        draft = drafts[doc_id]
        for level in LEVELS:
            for repeat in range(1, REPEATS + 1):
                run_id = f"{doc_id}-{level}-{repeat}"
                if run_id in finished:
                    continue
                if level == "E":
                    run_e(doc_id, repeat, draft)
                else:
                    run_f(doc_id, repeat, draft)
    summarize()


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPError as exc:
        raise SystemExit(f"llama-server 不可用：{exc}") from exc
