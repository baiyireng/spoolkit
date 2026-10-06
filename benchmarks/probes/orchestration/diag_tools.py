"""诊断：沙箱里那几个取数工具到底返回了什么（报告的证据，不是判据）。

    python diag_tools.py          # 用上一次 sandbox 阶段造的沙箱
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "src"))

from real_counterfactual import STATE_FILE, say  # noqa: E402
from counterfactual import _add_rg_to_path  # noqa: E402

from spoolkit.tools.registry import ToolRegistry  # noqa: E402
from spoolkit.tools.types import ToolCall  # noqa: E402
from spoolkit.tools.search import search_code_spec  # noqa: E402
from spoolkit.tools.fs import list_dir_spec, read_file_spec  # noqa: E402
from spoolkit.tools.survey import survey_spec  # noqa: E402
from spoolkit.llm.tokenizer import OfflineTokenCounter  # noqa: E402

project = Path(json.loads(STATE_FILE.read_text(encoding="utf-8"))["project"])
print(f"沙箱: {project}")
print(f"rg  : {_add_rg_to_path()}")

registry = ToolRegistry()
registry.register(list_dir_spec(project, None, ()))
registry.register(read_file_spec(project, None, ()))
registry.register(search_code_spec(project, None, ()))
registry.register(survey_spec(project, OfflineTokenCounter(), None, (), default_budget=1500))

calls = [
    ToolCall(name="list_dir", arguments={"path": "."}, intent="看根目录"),
    ToolCall(
        name="search_code",
        arguments={"pattern": "TODO|FIXME|XXX", "path": "src/spoolkit", "max_results": 8},
        intent="捞待办",
    ),
    ToolCall(
        name="search_code",
        arguments={"pattern": "orchestration-bench", "path": "src/spoolkit", "max_results": 8},
        intent="捞预置标记",
    ),
    ToolCall(
        name="read_file",
        arguments={"path": "src/spoolkit/cli/runtime.py", "start_line": 1, "end_line": 20},
        intent="读开头",
    ),
    ToolCall(
        name="search_code",
        arguments={"pattern": "stopped", "path": "src/spoolkit", "max_results": 8},
        intent="B 用",
    ),
]
for call in calls:
    result = registry.invoke(call)
    body = (result.content or "").replace("\n", " ⏎ ")
    print(f"\n--- {call.name}({json.dumps(call.arguments, ensure_ascii=False)}) ok={result.ok} "
          f"chars={len(result.content or '')} ---")
    print(body[:900])
