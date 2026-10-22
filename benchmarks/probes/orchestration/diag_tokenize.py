"""找 `/tokenize` 502 的触发点（不动 8080，只发只读请求）。"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8080"
client = httpx.Client(base_url=BASE, timeout=120.0, proxy=None)

sizes = [10, 100, 1000, 4000, 8000, 16000, 32000, 64000]
for size in sizes:
    text = ("这是一段用来试探 /tokenize 上限的中文文本。" * (size // 10 + 1))[:size]
    response = client.post("/tokenize", json={"content": text})
    print(f"chars={size:6d} status={response.status_code} body={response.text[:120]!r}")
    if response.status_code != 200:
        break

print("\n--- 非 ASCII 密度不变、换纯 ASCII ---")
for size in [8000, 16000, 32000]:
    text = ("abcdefghij" * (size // 10 + 1))[:size]
    response = client.post("/tokenize", json={"content": text})
    print(f"chars={size:6d} status={response.status_code} body={response.text[:120]!r}")

print("\n--- 拼一个真实装配的 system 文本 ---")
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from spoolkit.cli.runtime import LoopWiring, assemble_loop  # noqa: E402
from spoolkit.config import Config  # noqa: E402
from spoolkit.tools.edit import PendingChanges  # noqa: E402

repo = Path(__file__).resolve().parents[3]


class Null:
    def context_window(self):
        return 8192


loop = assemble_loop(
    repo, Null(), config=Config(project_root=repo, supervise=False, context_window=8192),
    wiring=LoopWiring(mcp=False, pending=PendingChanges(repo)),
)
from spoolkit.agent.loop import SYSTEM_PROMPT, build_workflow  # noqa: E402

text = SYSTEM_PROMPT.format(
    tools=loop.registry.describe(), workflow=build_workflow(loop.registry, ())
)
print(f"system chars={len(text)}")
response = client.post("/tokenize", json={"content": text})
print(f"system status={response.status_code} body={response.text[:200]!r}")
