"""P3-6 真机采样。

不改 protocol_v2 的默认值。只在这一次运行的 Config.overrides 里传入 0 或 1。
正文不写入 JSONL，也不写入报告。

用法：
  uv run python scripts/p3_live_metrics.py --arm v2 --limit 1
"""

from __future__ import annotations

import argparse
import json
import tempfile
import urllib.request
from dataclasses import replace
from datetime import date
from pathlib import Path

from spoolkit.agent.final_turn import FINAL_INSTRUCTION
from spoolkit.agent.long_regression import LONG_PROMPTS, LongPrompt
from spoolkit.agent.loop import AgentLoop
from spoolkit.agent.metrics import (
    capture_metrics,
    read_metrics,
    render_phase_report,
    render_section_cache_by_arm,
    write_metrics,
)
from spoolkit.config import Config
from spoolkit.llm.llamacpp import LlamaCppError, LlamaCppGateway
from spoolkit.llm.tokenizer import OfflineTokenCounter
from spoolkit.llm.types import ChatRequest
from spoolkit.tools.fs import list_dir_spec, read_file_spec
from spoolkit.tools.registry import ToolRegistry

ROOT = Path(__file__).resolve().parents[1]
SHORT_PROMPTS = tuple(
    LongPrompt("短答", f"用一句话回答：{number} 加 1 等于几？")
    for number in range(1, 21)
)
REQUIRED = 20
BASE_URL = "http://127.0.0.1:8080"


def _env() -> dict:
    with urllib.request.urlopen(BASE_URL + "/v1/models", timeout=5) as resp:
        payload = json.load(resp)
    meta = payload["data"][0]["meta"]
    return {
        "model": payload["data"][0]["id"],
        "quant": meta.get("ftype") or "",
        "n_ctx": int(meta.get("n_ctx") or 0),
        "slots": 1,
        "temperature": 0.0,
        "seed": 1,
    }


class _Wrap:
    """把 cache 或一次截断上限加在真实网关外面。生产路径不走这里。"""

    def __init__(
        self,
        inner: LlamaCppGateway,
        *,
        cache: bool | None = None,
        cap_needle: str | None = None,
        cap: int = 48,
    ) -> None:
        self._inner = inner
        self._cache = cache
        self._needle = cap_needle
        self._cap = cap
        self._capped = False

    def _shape(self, request: ChatRequest) -> ChatRequest:
        if self._cache is not None:
            request = replace(request, cache_prompt=self._cache)
        if self._needle and not self._capped:
            last = request.messages[-1].content if request.messages else ""
            if self._needle in last:
                self._capped = True
                request = replace(request, max_tokens=min(request.max_tokens, self._cap))
        return request

    def chat(self, request: ChatRequest):
        return self._inner.chat(self._shape(request))

    def chat_stream(self, request: ChatRequest, on_delta=None):
        return self._inner.chat_stream(self._shape(request), on_delta)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def _loop(root: Path, gateway, *, v2: bool) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(read_file_spec(root))
    registry.register(list_dir_spec(root))
    config = Config(
        project_root=root,
        context_window=8192,
            max_steps=12,
        supervise=False,
        overrides={"protocol_v2": 1 if v2 else 0},
    )
    return AgentLoop(
        gateway=gateway,
        tokenizer=OfflineTokenCounter(),
        registry=registry,
        config=config,
        compaction=False,
    )


def _run_one(prompt: str, gateway, *, v2: bool) -> tuple[list[dict], bool]:
    with tempfile.TemporaryDirectory(prefix="p3live-") as folder:
        loop = _loop(Path(folder), gateway, v2=v2)
        finished = False
        with capture_metrics() as rows:
            try:
                result = loop.run(prompt)
                finished = bool(result.finished)
            except LlamaCppError as exc:
                print(f"sample failed: {exc}", flush=True)
        tagged = [dict(row) for row in rows]
    return tagged, finished


def _arm_gateway(inner: LlamaCppGateway, arm: str):
    if arm == "cache-off":
        return _Wrap(inner, cache=False)
    if arm == "cache-on":
        return _Wrap(inner, cache=True)
    if arm == "section-cut":
        return _Wrap(inner, cap_needle="只写第")
    if arm == "final-cut":
        return _Wrap(inner, cap_needle=FINAL_INSTRUCTION[:8])
    return inner


def _v2_for(arm: str) -> bool:
    return arm != "legacy"


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) // 2


def _cell(value: object) -> str:
    if value is None:
        return "未采集"
    return str(value)


def write_report(rows: list[dict], env: dict, path: Path) -> None:
    turns = [row for row in rows if row.get("completed") is not None and not row.get("phase")]
    requests = [row for row in rows if row.get("phase")]
    lines = [
        "# P3-6 真机",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 模型 | `{env['model']}` |",
        f"| 量化 | {env['quant']} |",
        f"| n_ctx | {env['n_ctx']} |",
        f"| slot | {env['slots']} |",
        f"| temperature / seed | {env['temperature']} / {env['seed']} |",
        "| protocol_v2 默认 | 0。本报告的 v2 只存在这次运行的 overrides 里 |",
        "",
        "样本不足 20，或字段为空时，判定写「未采集」。",
        "",
        "## 分阶段",
        "",
        render_phase_report(requests),
        "",
    ]
    arm_cache = render_section_cache_by_arm(requests)
    if arm_cache.strip():
        lines.extend(["## section 缓存（分臂）", "", arm_cache, ""])
    lines.extend([
        "## 各臂",
        "",
        "| 臂 | 回合 | 完整率 | P50 总时延 ms | P50 prefill ms | P50 首 token ms | 截断率 |",
        "|---|---|---|---|---|---|---|",
    ])
    arms = sorted({str(row.get("arm")) for row in turns})
    by_arm: dict[str, list[dict]] = {arm: [] for arm in arms}
    for row in turns:
        by_arm[str(row.get("arm"))].append(row)
    req_by_turn = {}
    for row in requests:
        req_by_turn.setdefault(row.get("turn_id"), []).append(row)
    for arm in arms:
        group = by_arm[arm]
        done = sum(1 for row in group if row.get("completed"))
        rate = f"{done}/{len(group)}"
        latency = _median([int(row["total_ms"]) for row in group if row.get("total_ms") is not None])
        linked = [item for row in group for item in req_by_turn.get(row.get("turn_id"), [])]
        prefill = _median([int(item["prefill_ms"]) for item in linked if item.get("prefill_ms") is not None])
        first = _median([int(item["first_token_ms"]) for item in linked if item.get("first_token_ms") is not None])
        truncated = [item for item in linked if item.get("outcome") == "truncated"]
        cut = None if not linked else f"{len(truncated)}/{len(linked)}"
        lines.append(
            f"| {arm} | {len(group)}/{REQUIRED} | {rate} | {_cell(latency)} | {_cell(prefill)} | {_cell(first)} | {_cell(cut)} |"
        )
    lines.extend(["", "## 判定", ""])
    lines.append(_judge_cache(requests))
    lines.append(_judge_budget(requests))
    skipped = [
        row.get("long_skipped_reason")
        for row in turns
        if row.get("long_skipped_reason")
    ]
    if skipped:
        counts: dict[str, int] = {}
        for reason in skipped:
            counts[str(reason)] = counts.get(str(reason), 0) + 1
        lines.append("- long_skipped_reason：" + "，".join(f"{key} {value}" for key, value in sorted(counts.items())))
    else:
        lines.append("- long_skipped_reason：无")
    missing = sum(int(row.get("empty_sections") or 0) for row in turns)
    if missing:
        lines.append(f"- 内容缺失：empty_sections {missing}")
    else:
        lines.append("- 内容缺失：无")
    lines.append("")
    lines.append("变更单 §9.3：prefill 降幅 ≥70% 才保留双请求；<50% 回退单请求轻协议。单节 target 是上限：看不越界和中位数。不低于目标一半只做观察。")
    lines.append("样本未到 20 条时，上表的比率只是已跑样本，不能当成达标。")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def _judge_cache(requests: list[dict]) -> str:
    off = [row for row in requests if row.get("arm") == "cache-off" and row.get("phase") == "final" and row.get("prefill_ms") is not None]
    on = [row for row in requests if row.get("arm") == "cache-on" and row.get("phase") == "final" and row.get("prefill_ms") is not None]
    turns_off = {row.get("prompt_index") for row in off}
    turns_on = {row.get("prompt_index") for row in on}
    paired = turns_off & turns_on
    if len(paired) < REQUIRED:
        return (
            "- 缓存 prefill：长文路径不产生 final 请求；"
            "final 的 cache 成对样本需用短答臂采集。"
            f"（当前 final 成对 {len(paired)}/{REQUIRED}）"
        )
    drops = []
    for index in paired:
        base = _median([int(row["prefill_ms"]) for row in off if row.get("prompt_index") == index])
        hit = _median([int(row["prefill_ms"]) for row in on if row.get("prompt_index") == index])
        if base and hit is not None and base > 0:
            drops.append((base - hit) / base)
    if not drops:
        return "- 缓存 prefill：未采集"
    mean = sum(drops) / len(drops)
    if mean >= 0.70:
        verdict = "保留双请求"
    elif mean < 0.50:
        verdict = "回退单请求轻协议"
    else:
        verdict = "落在 50% 与 70% 之间，不改协议"
    return f"- 缓存 prefill 降幅：{mean:.0%}，{verdict}"


def _judge_budget(requests: list[dict]) -> str:
    sections = [
        row for row in requests
        if row.get("phase") == "section"
        and row.get("target_tokens")
        and row.get("generated_tokens") is not None
    ]
    turns = {row.get("prompt_index") for row in sections}
    if len(turns) < REQUIRED:
        return f"- 单节预算偏差：未采集（有 target_tokens 的长文样本 {len(turns)}/{REQUIRED}）"
    ratios = [
        int(row["generated_tokens"]) / int(row["target_tokens"])
        for row in sections
        if int(row["target_tokens"]) > 0
    ]
    if not ratios:
        return "- 单节预算：未采集"
    capped = sum(1 for row in sections if int(row["generated_tokens"]) <= int(row["target_tokens"]))
    floor = sum(
        1 for row in sections
        if int(row["generated_tokens"]) >= int(row["target_tokens"]) * 0.5
    )
    mid = _median([int(item * 100) for item in ratios])
    return (
        f"- 单节预算：generated/target 中位数 {mid}%。"
        f"不越界 {capped}/{len(sections)}。"
        "target 是上限，不是精确字数。"
        f"观察：不低于目标一半 {floor}/{len(sections)}，不作为判定。"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", default="v2", choices=("v2", "legacy", "cache-on", "cache-off", "section-cut", "final-cut"))
    parser.add_argument("--profile", default="long", choices=("long", "short"))
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--indexes", default="", help="逗号分隔的题号。给出后忽略 limit/offset")
    parser.add_argument("--jsonl", type=Path, default=None)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()
    env = _env()
    day = date.today().isoformat()
    jsonl = args.jsonl or (ROOT / "metrics" / f"{day}.jsonl")
    report = args.report or (ROOT / "docs" / "metrics" / f"{day}-report.md")
    gateway = LlamaCppGateway(base_url=BASE_URL, temperature=0.0, seed=1, timeout=180.0)
    wrapped = _arm_gateway(gateway, args.arm)
    source = LONG_PROMPTS if args.profile == "long" else SHORT_PROMPTS
    index_base = 0 if args.profile == "long" else 100
    if args.indexes.strip():
        selected = [(int(item), source[int(item)]) for item in args.indexes.split(",") if item.strip()]
    else:
        start = max(0, args.offset)
        selected = [
            (index, prompt)
            for index, prompt in enumerate(source[: max(0, args.limit)])
            if index >= start
        ]
    try:
        for index, prompt in selected:
            rows, finished = _run_one(prompt.text, wrapped, v2=_v2_for(args.arm))
            for row in rows:
                row["arm"] = args.arm
                row["prompt_index"] = index_base + index
                row["category"] = prompt.category
            write_metrics(jsonl, rows)
            print(f"{args.arm} {args.profile} #{index} finished={finished} rows={len(rows)}", flush=True)
    finally:
        gateway.close()
    write_report(read_metrics(jsonl), env, report)
    print(report)


if __name__ == "__main__":
    main()
