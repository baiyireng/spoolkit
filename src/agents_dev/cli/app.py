"""最小命令行入口。

当前阶段用脚本化假模型驱动，因此整个闭环在无 GPU 环境下即可运行。
接入 llama.cpp 后，只需把 build_loop 中的 FakeModel 换成真实网关。
"""

import argparse
import json
import sys
from pathlib import Path

from agents_dev.agent.loop import AgentLoop
from agents_dev.config import Config
from agents_dev.llm.fake import FakeModel
from agents_dev.llm.tokenizer import OfflineTokenCounter
from agents_dev.tools.fs import list_dir_spec, read_file_spec
from agents_dev.tools.registry import ToolRegistry
from agents_dev.tools.search import search_code_spec


def build_loop(project_root: Path, script: list[str], window: int = 4096) -> AgentLoop:
    """装配一个由假模型驱动的完整循环。"""
    registry = ToolRegistry()
    registry.register(read_file_spec(project_root))
    registry.register(list_dir_spec(project_root))
    registry.register(search_code_spec(project_root))

    tokenizer = OfflineTokenCounter()
    config = Config(project_root=project_root, context_window=window)
    return AgentLoop(
        gateway=FakeModel(script=script, tokenizer=tokenizer),
        tokenizer=tokenizer,
        registry=registry,
        config=config,
    )


def _run(args: argparse.Namespace) -> int:
    script_path = Path(args.script)
    if not script_path.exists():
        print(f"脚本文件不存在: {script_path}", file=sys.stderr)
        return 2

    raw = json.loads(script_path.read_text(encoding="utf-8"))
    script = [
        item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        for item in raw
    ]

    loop = build_loop(Path(args.root).resolve(), script=script, window=args.window)
    result = loop.run(args.goal)

    for line in result.trace:
        print(line)
    print("---")
    print(result.final)
    return 0 if result.finished else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agents-dev")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="运行一次任务")
    run_parser.add_argument("--goal", required=True)
    run_parser.add_argument("--script", required=True, help="假模型脚本 JSON")
    run_parser.add_argument("--root", default=".")
    run_parser.add_argument("--window", type=int, default=4096)
    run_parser.set_defaults(func=_run)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

