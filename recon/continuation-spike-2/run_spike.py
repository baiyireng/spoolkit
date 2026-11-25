"""L0.4：结构状态消融，以及分节预算复测。不进产品路径。

D0–D3 共用一份截断初稿。E 不续写截断初稿，按节独立生成。
温度 0、seed=1、n_ctx=8192。预算用 OfflineTokenCounter，留 192 余量。
服务端模板默认会先写长思考，请求里固定 enable_thinking=false。
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
DRAFT_CAP = 1600
CONTINUE_CAP = 1500
MAX_CONTINUE_ROUNDS = 3
REPEATS = 3
TEMPERATURE = 0
SEED = 1
CEILING = N_CTX - MARGIN

COUNTER = OfflineTokenCounter()
NGRAM = 12
REPEAT_WINDOW = 400

HEADINGS = ("问题", "约束", "做法", "失败", "取舍", "结论")
POINTS = {
    "问题": "一次生成为什么会在 8K 窗口里写到一半停住",
    "约束": "窗口、截断标记、不能把整篇再塞回提示词",
    "做法": "无尾部提示、带尾部续写、按节独立写，各自的输入是什么",
    "失败": "重复已写段落、重新开头、把后半写成简略版",
    "取舍": "什么时候值得续写，什么时候应该拆成节",
    "结论": "给一个工程师能照做的建议",
}

SYSTEM = (
    "你是技术写作者。只输出 Markdown 正文。"
    "不要输出思考过程，不要调用工具，不要解释你在做什么。"
)

TASK = (
    "请写一篇中文技术说明，题目是「8K 窗口里怎么把一份中文长文写完」。"
    "读者是工程师。每节至少四个段落，每节不少于 700 字，要有具体例子，不要用提纲代替正文。"
    "六节都写完以后，最后一行单独写「完」。"
)

OUTLINE = "\n".join(f"## {title}\n{POINTS[title]}" for title in HEADINGS)
CONTINUE_RULE = "只输出紧接其后的新增内容，不要重复，不要重新开头，不要总结。"


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


def last_heading(text: str) -> str:
    found = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            found = stripped[3:].strip()
    return found or "（没有标题）"


def paragraph_digest(text: str) -> str:
    parts = [part.strip() for part in text.split("\n\n") if part.strip()]
    if not parts:
        return "（空）"
    last = parts[-1].replace("\n", "")
    if len(last) <= 80:
        return last
    return last[:80] + "…"


def unfinished(text: str) -> list[str]:
    return [title for title in HEADINGS if f"## {title}" not in text]


def structure(text: str) -> dict[str, object]:
    found = [title for title in HEADINGS if f"## {title}" in text]
    return {
        "headings": found,
        "heading_count": len(found),
        "has_ending": text.strip().endswith("完"),
        "has_paragraphs": len([part for part in text.split("\n\n") if part.strip()]) >= 4,
        "complete": len(found) == len(HEADINGS) and text.strip().endswith("完"),
    }


def ngrams(text: str) -> list[str]:
    if len(text) < NGRAM:
        return []
    return [text[index : index + NGRAM] for index in range(len(text) - NGRAM + 1)]


def ngram_repeat_ratio(text: str) -> float:
    grams = ngrams(text)
    if not grams:
        return 0.0
    counts = Counter(grams)
    repeated = sum(count for count in counts.values() if count > 1)
    return repeated / len(grams)


def overlap_ratio(left: str, right: str) -> float:
    """right 的 12 字片段有多少已经出现在 left 里。"""
    right_grams = ngrams(right)
    if not right_grams:
        return 0.0
    left_grams = set(ngrams(left))
    return sum(1 for gram in right_grams if gram in left_grams) / len(right_grams)


def looks_repeated(head: str, tail: str) -> bool:
    if len(tail) < NGRAM or len(head) < NGRAM:
        return False
    right = ngrams(tail[:REPEAT_WINDOW])
    left = set(ngrams(head[-REPEAT_WINDOW:]))
    if not right:
        return False
    return sum(1 for gram in right if gram in left) / len(right) > 0.5


def draft_messages() -> list[dict[str, str]]:
    user = (
        f"{TASK}\n\n按这个顺序写，不要先交一份只有标题的提纲：\n{OUTLINE}\n\n"
        "从「## 问题」写起。"
    )
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user},
    ]


def ablation_messages(level: str, draft: str) -> list[dict[str, str]]:
    lines = [f"大纲：\n{OUTLINE}"]
    if level in {"D1", "D2", "D3"}:
        lines.append(f"最后标题：{last_heading(draft)}")
    if level in {"D2", "D3"}:
        lines.append(f"最后一段摘要：{paragraph_digest(draft)}")
    if level == "D3":
        missing = "、".join(unfinished(draft)) or "（标题都已出现）"
        lines.append(f"未完成：{missing}")
    lines.append(CONTINUE_RULE + "写完后最后一行单独写「完」。")
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": "\n\n".join(lines)},
    ]


def section_messages(title: str, previous: str) -> list[dict[str, str]]:
    user = (
        f"{TASK}\n\n全文大纲：\n{OUTLINE}\n\n"
        f"现在只写这一节：## {title}\n要点：{POINTS[title]}\n"
        f"上一节结论摘要：{previous or '（这是第一节，没有上一节）'}\n"
        "不要写其他节，不要写下一个标题。本节不少于 700 字。"
        "本节最后一行写「本节结论：」加一句话。"
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
    timeout = max(180.0, float(max_tokens))
    with httpx.Client(timeout=timeout, trust_env=False) as client:
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
    found: set[str] = set()
    for line in RUNS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            found.add(str(json.loads(line)["run_id"]))
    return found


def call(
    *,
    run_id: str,
    strategy: str,
    repeat: int,
    phase: str,
    round_index: int,
    messages: list[dict[str, str]],
    cap: int,
    section: str = "",
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
    within = actual == 0 or actual + max_tokens <= CEILING
    result["max_tokens"] = max_tokens
    result["within_budget"] = within and estimate_prompt + max_tokens <= CEILING
    append(
        REQUESTS,
        {
            "run_id": run_id,
            "strategy": strategy,
            "repeat": repeat,
            "phase": phase,
            "round": round_index,
            "section": section,
            "max_tokens": max_tokens,
            "estimate_prompt_tokens": estimate_prompt,
            "prompt_tokens": actual,
            "completion_tokens": result["completion_tokens"],
            "truncated": result["truncated"],
            "finish_reason": result["finish_reason"],
            "ttft_s": result["ttft_s"],
            "elapsed_s": result["elapsed_s"],
            "estimate_minus_actual": estimate_prompt - actual,
            "within_budget": result["within_budget"],
            "prompt_plus_max": actual + max_tokens,
            "ceiling": CEILING,
            "messages": messages,
            "text": result["text"],
        },
    )
    print(
        f"  tokens={actual}+{result['completion_tokens']} "
        f"truncated={result['truncated']} within={result['within_budget']} "
        f"{result['elapsed_s']}s",
        flush=True,
    )
    return result


def load_draft() -> tuple[str, dict[str, object]] | None:
    if not REQUESTS.exists():
        return None
    found: dict[str, object] | None = None
    for line in REQUESTS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if item["phase"] == "draft":
            found = item
    if found is None:
        return None
    call_row = {
        "text": found["text"],
        "prompt_tokens": found["prompt_tokens"],
        "completion_tokens": found["completion_tokens"],
        "truncated": found["truncated"],
        "ttft_s": found["ttft_s"],
        "elapsed_s": found["elapsed_s"],
        "max_tokens": found["max_tokens"],
        "budget_blocked": False,
        "within_budget": found["within_budget"],
    }
    return str(found["text"]), call_row


def finish_run(row: dict[str, object]) -> None:
    append(RUNS, row)
    print(
        f"DONE {row['run_id']} complete={row['complete']} "
        f"repeat={row['ngram_repeat_ratio']} tokens={row['total_tokens']} "
        f"{row['elapsed_s']}s",
        flush=True,
    )


def continue_run(level: str, repeat: int, draft: str, draft_call: dict[str, object]) -> None:
    run_id = f"{level}-{repeat}"
    text = draft
    calls = [draft_call]
    repeated_rounds = 0
    rounds = 0
    while rounds < MAX_CONTINUE_ROUNDS and not structure(text)["complete"]:
        result = call(
            run_id=run_id,
            strategy=level,
            repeat=repeat,
            phase="continuation",
            round_index=rounds + 1,
            messages=ablation_messages(level, text),
            cap=CONTINUE_CAP,
        )
        calls.append(result)
        rounds += 1
        piece = str(result["text"])
        if piece and looks_repeated(text, piece):
            repeated_rounds += 1
        if result.get("budget_blocked") or not piece.strip():
            break
        text += "\n\n" + piece
        if not result["truncated"] and structure(text)["complete"]:
            break
    info = structure(text)
    prompt_tokens = sum(int(item["prompt_tokens"]) for item in calls)
    completion_tokens = sum(int(item["completion_tokens"]) for item in calls)
    finish_run(
        {
            "run_id": run_id,
            "strategy": level,
            "repeat": repeat,
            "complete": bool(info["complete"]) and not any(item.get("budget_blocked") for item in calls),
            "heading_count": info["heading_count"],
            "headings": info["headings"],
            "has_ending": info["has_ending"],
            "ngram_repeat_ratio": round(ngram_repeat_ratio(text), 4),
            "repeated_rounds": repeated_rounds,
            "continuation_rounds": rounds,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "elapsed_s": round(sum(float(item["elapsed_s"]) for item in calls), 3),
            "within_budget": all(bool(item.get("within_budget", True)) for item in calls),
            "chars": len(text),
            "text": text,
        }
    )


def section_ok(title: str, piece: str, result: dict[str, object]) -> bool:
    if result.get("budget_blocked") or result["truncated"] or not piece.strip():
        return False
    has_title = f"## {title}" in piece or piece.strip().startswith(title)
    return has_title and "本节结论：" in piece


def section_run(cap: int, repeat: int) -> None:
    strategy = f"E{cap}"
    run_id = f"{strategy}-{repeat}"
    parts: list[str] = []
    calls: list[dict[str, object]] = []
    sections: list[dict[str, object]] = []
    previous = ""
    previous_text = ""
    ok = 0
    for index, title in enumerate(HEADINGS, start=1):
        result = call(
            run_id=run_id,
            strategy=strategy,
            repeat=repeat,
            phase="section",
            round_index=index,
            messages=section_messages(title, previous),
            cap=cap,
            section=title,
        )
        calls.append(result)
        piece = str(result["text"])
        parts.append(piece)
        done = section_ok(title, piece, result)
        if done:
            ok += 1
        line = ""
        for row in piece.splitlines():
            if row.strip().startswith("本节结论："):
                line = row.strip()
        sections.append(
            {
                "title": title,
                "truncated": bool(result["truncated"]),
                "has_conclusion": "本节结论：" in piece,
                "complete": done,
                "completion_tokens": int(result["completion_tokens"]),
                "chars": len(piece),
                "overlap_with_previous": round(overlap_ratio(previous_text, piece), 4) if previous_text else 0.0,
                "looks_repeated": bool(previous_text) and looks_repeated(previous_text, piece),
            }
        )
        previous = line or ""
        previous_text = piece
    joined = "\n\n".join(parts)
    prompt_tokens = sum(int(item["prompt_tokens"]) for item in calls)
    completion_tokens = sum(int(item["completion_tokens"]) for item in calls)
    finish_run(
        {
            "run_id": run_id,
            "strategy": strategy,
            "repeat": repeat,
            "section_cap": cap,
            "complete": ok == len(HEADINGS),
            "heading_count": ok,
            "sections": sections,
            "truncated_sections": sum(1 for item in sections if item["truncated"]),
            "conclusion_sections": sum(1 for item in sections if item["has_conclusion"]),
            "ngram_repeat_ratio": round(ngram_repeat_ratio(joined), 4),
            "mean_section_overlap": round(
                sum(float(item["overlap_with_previous"]) for item in sections[1:]) / max(1, len(sections) - 1),
                4,
            ),
            "repeated_sections": sum(1 for item in sections if item["looks_repeated"]),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "elapsed_s": round(sum(float(item["elapsed_s"]) for item in calls), 3),
            "within_budget": all(bool(item.get("within_budget", True)) for item in calls),
            "chars": len(joined),
            "text": joined,
        }
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
        grouped.setdefault(str(row["strategy"]), []).append(row)
    report: dict[str, object] = {
        "runs": len(rows),
        "requests": len(requests),
        "within_budget": sum(1 for item in requests if item["within_budget"]),
        "over_budget": sum(1 for item in requests if not item["within_budget"]),
        "strategies": {},
    }
    for strategy, items in sorted(grouped.items()):
        count = len(items)
        block: dict[str, object] = {
            "n": count,
            "complete": sum(1 for item in items if item["complete"]),
            "mean_total_tokens": round(sum(int(item["total_tokens"]) for item in items) / count, 1),
            "mean_elapsed_s": round(sum(float(item["elapsed_s"]) for item in items) / count, 1),
            "mean_ngram_repeat": round(sum(float(item["ngram_repeat_ratio"]) for item in items) / count, 4),
            "within_budget": sum(1 for item in items if item["within_budget"]),
        }
        if "sections" in items[0]:
            block["mean_truncated_sections"] = round(
                sum(int(item["truncated_sections"]) for item in items) / count, 2
            )
            block["mean_conclusion_sections"] = round(
                sum(int(item["conclusion_sections"]) for item in items) / count, 2
            )
            block["mean_section_overlap"] = round(
                sum(float(item["mean_section_overlap"]) for item in items) / count, 4
            )
        report["strategies"][strategy] = block
    SUMMARY.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


def main() -> None:
    assert estimate("甲" * 2000) == 2000
    finished = done_ids()
    loaded = load_draft()
    if loaded is None:
        result = call(
            run_id="draft-1",
            strategy="draft",
            repeat=1,
            phase="draft",
            round_index=0,
            messages=draft_messages(),
            cap=DRAFT_CAP,
        )
        draft = str(result["text"])
        print(f"draft chars={len(draft)} truncated={result['truncated']}", flush=True)
        if not result["truncated"] or len(draft) < 2200:
            raise SystemExit(
                "初稿没有形成可消融的截断尾部："
                f"chars={len(draft)} truncated={result['truncated']}"
            )
        draft_call = result
    else:
        draft, draft_call = loaded
        print(f"reuse draft chars={len(draft)}", flush=True)
    for repeat in range(1, REPEATS + 1):
        for level in ("D0", "D1", "D2", "D3"):
            if f"{level}-{repeat}" not in finished:
                continue_run(level, repeat, draft, draft_call)
        for cap in (1200, 1600):
            if f"E{cap}-{repeat}" not in finished:
                section_run(cap, repeat)
    summarize()


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPError as exc:
        raise SystemExit(f"llama-server 不可用：{exc}") from exc
