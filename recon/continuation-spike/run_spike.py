"""续写策略对照。不进产品路径。

固定条件：同一本地模型、temperature=0、seed=1、n_ctx=8192。
预算用 OfflineTokenCounter（ASCII 4 字 1 token，非 ASCII 1 字 1 token），
再留 192 的余量。服务端模板默认会打开长思考，思考会先吃掉窗口，
所以请求里固定 enable_thinking=false，比较的是正文续写而不是推理预算。
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
SECTION_CAP = 800
MAX_CONTINUE_ROUNDS = 3
REPEATS = 3
TEMPERATURE = 0
SEED = 1

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

CONTINUE_RULE = (
    "只输出紧接其后的新增内容，不要重复，不要重新开头，不要总结。"
)


def estimate(text: str) -> int:
    return COUNTER.count(text)


def message_tokens(messages: list[dict[str, str]]) -> int:
    return estimate("\n".join(item["content"] for item in messages))


def output_budget(messages: list[dict[str, str]], cap: int) -> tuple[int, int]:
    prompt = message_tokens(messages)
    room = N_CTX - MARGIN - prompt
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


def last_sentence(text: str) -> str:
    chunk = text.strip().replace("\n", "")
    if not chunk:
        return "（空）"
    for mark in ("。", "！", "？"):
        if mark in chunk:
            piece = chunk.rsplit(mark, 1)[0]
            # 取最后一个完整句；若截断在句中，就用末尾 40 字。
            prior = piece.rsplit("。", 1)[-1].rsplit("！", 1)[-1].rsplit("？", 1)[-1]
            sentence = (prior or piece)[-40:] + mark
            if sentence.strip(mark):
                return sentence
    return chunk[-40:]


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
    ending = text.strip().endswith("完")
    paragraphs = len([part for part in text.split("\n\n") if part.strip()]) >= 4
    return {
        "headings": found,
        "heading_count": len(found),
        "has_ending": ending,
        "has_paragraphs": paragraphs,
        "complete": len(found) == len(HEADINGS) and ending and paragraphs,
    }


def ngram_repeat_ratio(text: str) -> float:
    if len(text) < NGRAM:
        return 0.0
    grams = [text[index : index + NGRAM] for index in range(len(text) - NGRAM + 1)]
    counts = Counter(grams)
    repeated = sum(count for count in counts.values() if count > 1)
    return repeated / len(grams)


def looks_repeated(head: str, tail: str) -> bool:
    if len(tail) < NGRAM or len(head) < NGRAM:
        return False
    right = [tail[index : index + NGRAM] for index in range(min(len(tail), REPEAT_WINDOW) - NGRAM + 1)]
    if not right:
        return False
    left = set(
        head[index : index + NGRAM]
        for index in range(max(0, len(head) - REPEAT_WINDOW), max(0, len(head) - NGRAM + 1))
    )
    # 窗口不足 12 字时上面的 range 可能为空。
    if len(head) >= NGRAM and not left:
        left = {head[-NGRAM:]}
    shared = sum(1 for gram in right if gram in left)
    return shared / len(right) > 0.5


def tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def strategy_messages(kind: str, draft: str, *, section: str = "", previous: str = "") -> list[dict[str, str]]:
    if kind == "E":
        user = (
            f"{TASK}\n\n全文大纲：\n{OUTLINE}\n\n"
            f"现在只写这一节：## {section}\n要点：{POINTS[section]}\n"
            f"上一节结论摘要：{previous or '（这是第一节，没有上一节）'}\n"
            "不要写其他节，不要写下一个标题。"
            "本节最后一行写「本节结论：」加一句话。"
        )
        return [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user},
        ]
    if kind == "A":
        user = (
            f"大纲：\n{OUTLINE}\n\n"
            f"最后标题：{last_heading(draft)}\n"
            f"最后一句：{last_sentence(draft)}\n"
            f"{CONTINUE_RULE}"
        )
        return [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user},
        ]
    if kind in {"B", "C"}:
        limit = 500 if kind == "B" else 2000
        return [
            {"role": "system", "content": SYSTEM},
            {"role": "assistant", "content": tail(draft, limit)},
            {"role": "user", "content": "以上是你已经写出的最后一段。" + CONTINUE_RULE},
        ]
    if kind == "D":
        missing = "、".join(unfinished(draft)) or "（标题都已出现）"
        user = (
            f"大纲：\n{OUTLINE}\n\n"
            f"最后标题：{last_heading(draft)}\n"
            f"最后一段摘要：{paragraph_digest(draft)}\n"
            f"未完成：{missing}\n"
            f"{CONTINUE_RULE}写完后最后一行单独写「完」。"
        )
        return [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user},
        ]
    raise ValueError(kind)


def draft_messages() -> list[dict[str, str]]:
    user = (
        f"{TASK}\n\n按这个顺序写，不要先交一份只有标题的提纲：\n{OUTLINE}\n\n"
        "从「## 问题」写起。"
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
    timeout = max(120.0, float(max_tokens))
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
    text = "".join(pieces)
    return {
        "text": text,
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
        if not line.strip():
            continue
        found.add(str(json.loads(line)["run_id"]))
    return found


def record_call(
    *,
    run_id: str,
    strategy: str,
    repeat: int,
    phase: str,
    round_index: int,
    messages: list[dict[str, str]],
    max_tokens: int,
    result: dict[str, object],
) -> None:
    estimate_prompt = message_tokens(messages)
    actual = int(result["prompt_tokens"])
    append(
        REQUESTS,
        {
            "run_id": run_id,
            "strategy": strategy,
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
            "messages": messages,
            "text": result["text"],
        },
    )


def call(
    *,
    run_id: str,
    strategy: str,
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
        result = {
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
    result["max_tokens"] = max_tokens
    record_call(
        run_id=run_id,
        strategy=strategy,
        repeat=repeat,
        phase=phase,
        round_index=round_index,
        messages=messages,
        max_tokens=max_tokens,
        result=result,
    )
    print(
        f"  tokens={result['prompt_tokens']}+{result['completion_tokens']} "
        f"truncated={result['truncated']} {result['elapsed_s']}s",
        flush=True,
    )
    return result


def continue_run(strategy: str, repeat: int, draft: str, draft_calls: list[dict[str, object]]) -> None:
    run_id = f"{strategy}-{repeat}"
    text = draft
    calls = list(draft_calls)
    repeated_rounds = 0
    rounds = 0
    while rounds < MAX_CONTINUE_ROUNDS and not structure(text)["complete"]:
        messages = strategy_messages(strategy, text)
        result = call(
            run_id=run_id,
            strategy=strategy,
            repeat=repeat,
            phase="continuation",
            round_index=rounds + 1,
            messages=messages,
            cap=CONTINUE_CAP,
        )
        calls.append(result)
        rounds += 1
        piece = str(result["text"])
        if piece and looks_repeated(text, piece):
            repeated_rounds += 1
        if result.get("budget_blocked") or not piece.strip():
            break
        text += piece
        if not result["truncated"] and structure(text)["complete"]:
            break
        if not result["truncated"] and structure(text)["heading_count"] == structure(text + piece)["heading_count"]:
            # 没被截断却没把结构补上，再续一轮仍用同一策略；轮数有上限。
            pass
    finish_run(run_id, strategy, repeat, text, calls, repeated_rounds, rounds)


def section_is_complete(title: str, piece: str, result: dict[str, object]) -> bool:
    if result.get("budget_blocked") or result["truncated"] or not piece.strip():
        return False
    has_title = f"## {title}" in piece or piece.strip().startswith(title)
    return has_title and "本节结论：" in piece


def section_run(repeat: int) -> None:
    run_id = f"E-{repeat}"
    parts: list[str] = []
    calls: list[dict[str, object]] = []
    previous = ""
    repeated_rounds = 0
    section_ok = 0
    for index, title in enumerate(HEADINGS, start=1):
        messages = strategy_messages("E", "", section=title, previous=previous)
        result = call(
            run_id=run_id,
            strategy="E",
            repeat=repeat,
            phase="section",
            round_index=index,
            messages=messages,
            cap=SECTION_CAP,
        )
        calls.append(result)
        piece = str(result["text"])
        if previous and looks_repeated(previous, piece):
            repeated_rounds += 1
        parts.append(piece)
        if section_is_complete(title, piece, result):
            section_ok += 1
        line = ""
        for row in piece.splitlines():
            if row.strip().startswith("本节结论："):
                line = row.strip()
        previous = line or last_sentence(piece)
    finish_run(
        run_id,
        "E",
        repeat,
        "\n\n".join(parts),
        calls,
        repeated_rounds,
        0,
        complete=section_ok == len(HEADINGS),
        heading_count=section_ok,
    )


def finish_run(
    run_id: str,
    strategy: str,
    repeat: int,
    text: str,
    calls: list[dict[str, object]],
    repeated_rounds: int,
    continuation_rounds: int,
    *,
    complete: bool | None = None,
    heading_count: int | None = None,
) -> None:
    info = structure(text)
    prompt_tokens = sum(int(item["prompt_tokens"]) for item in calls)
    completion_tokens = sum(int(item["completion_tokens"]) for item in calls)
    elapsed = round(sum(float(item["elapsed_s"]) for item in calls), 3)
    ttft = [float(item["ttft_s"]) for item in calls]
    done = bool(info["complete"]) if complete is None else complete
    if any(item.get("budget_blocked") for item in calls):
        done = False
    row = {
        "run_id": run_id,
        "strategy": strategy,
        "repeat": repeat,
        "complete": done,
        "heading_count": info["heading_count"] if heading_count is None else heading_count,
        "headings": info["headings"],
        "has_ending": info["has_ending"] if strategy != "E" else done,
        "ngram_repeat_ratio": round(ngram_repeat_ratio(text), 4),
        "repeated_rounds": repeated_rounds,
        "continuation_rounds": continuation_rounds,
        "continuation_max_tokens": [
            int(item["max_tokens"])
            for item in calls
            if item.get("phase") == "continuation" or (
                strategy != "E" and calls.index(item) > 0
            )
        ],
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "ttft_s": ttft,
        "elapsed_s": elapsed,
        "calls": len(calls),
        "chars": len(text),
        "text": text,
    }
    # call() 没有把 phase 写回 result。续写的 max_tokens 从请求日志回填。
    row["continuation_max_tokens"] = continuation_limits(run_id)
    append(RUNS, row)
    print(
        f"DONE {run_id} complete={row['complete']} headings={row['heading_count']} "
        f"repeat={row['ngram_repeat_ratio']} tokens={row['total_tokens']} {elapsed}s",
        flush=True,
    )


def continuation_limits(run_id: str) -> list[int]:
    limits: list[int] = []
    if not REQUESTS.exists():
        return limits
    for line in REQUESTS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if item["run_id"] == run_id and item["phase"] in {"continuation", "section"}:
            limits.append(int(item["max_tokens"]))
    return limits


def load_drafts() -> dict[int, tuple[str, list[dict[str, object]]]]:
    found: dict[int, tuple[str, list[dict[str, object]]]] = {}
    if not REQUESTS.exists():
        return found
    grouped: dict[int, dict[str, object]] = {}
    for line in REQUESTS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if item["phase"] != "draft":
            continue
        grouped[int(item["repeat"])] = item
    for repeat, item in grouped.items():
        call_row = {
            "text": item["text"],
            "prompt_tokens": item["prompt_tokens"],
            "completion_tokens": item["completion_tokens"],
            "truncated": item["truncated"],
            "ttft_s": item["ttft_s"],
            "elapsed_s": item["elapsed_s"],
            "max_tokens": item["max_tokens"],
            "budget_blocked": False,
        }
        found[repeat] = (str(item["text"]), [call_row])
    return found


def summarize() -> None:
    rows = [
        json.loads(line)
        for line in RUNS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    by_strategy: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        by_strategy.setdefault(str(row["strategy"]), []).append(row)
    report: dict[str, object] = {"runs": len(rows), "strategies": {}}
    for strategy, items in sorted(by_strategy.items()):
        count = len(items)
        report["strategies"][strategy] = {
            "n": count,
            "complete": sum(1 for item in items if item["complete"]),
            "mean_total_tokens": round(sum(int(item["total_tokens"]) for item in items) / count, 1),
            "mean_elapsed_s": round(sum(float(item["elapsed_s"]) for item in items) / count, 1),
            "mean_ttft_s": round(
                sum(sum(item["ttft_s"]) / len(item["ttft_s"]) for item in items) / count,
                3,
            ),
            "mean_ngram_repeat": round(
                sum(float(item["ngram_repeat_ratio"]) for item in items) / count,
                4,
            ),
            "mean_repeated_rounds": round(
                sum(int(item["repeated_rounds"]) for item in items) / count,
                2,
            ),
            "mean_chars": round(sum(int(item["chars"]) for item in items) / count, 1),
            "mean_headings": round(sum(int(item["heading_count"]) for item in items) / count, 2),
            "ending": sum(1 for item in items if item["has_ending"]),
        }
    SUMMARY.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


def main() -> None:
    assert estimate("甲" * 2000) == 2000
    assert estimate("甲" * 2000) != 1250
    finished = done_ids()
    drafts = load_drafts()
    for repeat in range(1, REPEATS + 1):
        if repeat not in drafts:
            messages = draft_messages()
            result = call(
                run_id=f"draft-{repeat}",
                strategy="draft",
                repeat=repeat,
                phase="draft",
                round_index=0,
                messages=messages,
                cap=DRAFT_CAP,
            )
            draft_text = str(result["text"])
            print(
                f"draft-{repeat} chars={len(draft_text)} truncated={result['truncated']}",
                flush=True,
            )
            if not result["truncated"] or len(draft_text) < 2200:
                raise SystemExit(
                    "初稿没有形成可比较的截断尾部："
                    f"chars={len(draft_text)} truncated={result['truncated']}。"
                    "A/B/C 需要一份长于 2000 字、并且被截断的 partial。"
                )
            drafts[repeat] = (draft_text, [result])
        draft, draft_calls = drafts[repeat]
        for strategy in ("A", "B", "C", "D"):
            run_id = f"{strategy}-{repeat}"
            if run_id in finished:
                continue
            continue_run(strategy, repeat, draft, draft_calls)
        if f"E-{repeat}" not in finished:
            section_run(repeat)
    summarize()


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPError as exc:
        raise SystemExit(f"llama-server 不可用：{exc}") from exc
