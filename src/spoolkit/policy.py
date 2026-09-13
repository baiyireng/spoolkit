"""授权策略。

策略是「自动化到哪一步」的开关，不是「信不信任模型」。所以每一档都必须
说清楚它把自动化限定在哪里——**没有边界的自动等于没有退路**。

三档：
- `ask`：每次写操作都要确认。默认，最保守。
- `auto`：改动落在允许范围内就自动落盘；越界退回确认。
- `deny`：只读。改动会被丢弃，绝不落盘。

策略持久化在 .agent/policy.json。这样「做完一轮再调整」是有意义的：
调整会留在那里，下一次推进自动沿用，不需要每条命令都带上开关。
"""

import json
from pathlib import Path

ASK = "ask"
AUTO = "auto"
DENY = "deny"

POLICIES = (ASK, AUTO, DENY)
DEFAULT_POLICY = ASK

DESCRIPTIONS = {
    ASK: "每次写操作都要确认（默认，最保守）",
    AUTO: "改动落在允许范围内自动落盘，越界退回确认",
    DENY: "只读模式：改动会被丢弃，绝不落盘",
}


def describe(policy: str) -> str:
    return DESCRIPTIONS.get(policy, f"未知策略 {policy}")


def policy_path(project_root: Path) -> Path:
    return project_root / ".agent" / "policy.json"


def load_policy(path: Path) -> str:
    """读取已保存的策略。读不到或内容非法时退回默认档。"""
    if not path.exists():
        return DEFAULT_POLICY
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("policy")
    except (json.JSONDecodeError, AttributeError):
        return DEFAULT_POLICY
    return value if value in POLICIES else DEFAULT_POLICY


def save_policy(path: Path, policy: str) -> None:
    if policy not in POLICIES:
        raise ValueError(f"未知策略: {policy}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"policy": policy}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

