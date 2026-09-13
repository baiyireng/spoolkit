"""配对：默认不认识的人不能用，而不是默认谁都能用。

原先是"没配白名单就等于谁都能驱动这个工作区"——那只打一句警告。聊天通道挂在
公网上，"默认放行"是致命的（OpenClaw 的微信插件就在这上面栽过：它自己的文档
写着 2.4.8 访问控制有缺陷，名单空了会放行任何发送者）。

所以默认改成**配对**：陌生发送者拿到一个一次性配对码，回复里告诉他把码给主人；
主人在机器上执行 `agents-dev bridge --approve <码>` 之后，这个人才被放行。

三档策略，由调用方显式选：

- `pairing`（默认）：配对过的放行，其它人给码；
- `allowlist`：只放行名单里的人（名单外连码都不给）；
- `open`：谁都放行（**要显式选**，并且在启动时警告）。

状态落在 `.agent/bridge-pairings.json`：这是工作区级的东西（这台机器上谁能
驱动这个工作区），和 `.agent/policy.json` 同类。
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path

PAIRING = "pairing"
ALLOWLIST = "allowlist"
OPEN = "open"

POLICIES = (PAIRING, ALLOWLIST, OPEN)

# 配对码的字母表刻意去掉 0/O、1/I/L：这串码要靠人在聊天里念、在终端里敲。
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LENGTH = 6


class Pairings:
    """已批准的人 + 待批准的码。文件读不了就当空的（配对是增强，不挡住启动）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._approved: set[str] = set()
        self._codes: dict[str, str] = {}  # code -> user
        self._load()

    # --- 读写 ---

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self._approved = {str(item) for item in payload.get("approved") or []}
        codes = payload.get("codes") or {}
        self._codes = {str(code): str(user) for code, user in codes.items()}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "approved": sorted(self._approved),
                    "codes": self._codes,
                    "updated_at": time.time(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    # --- 查询与变更 ---

    def is_approved(self, user: str) -> bool:
        return str(user) in self._approved

    def approve(self, user: str) -> None:
        user = str(user)
        self._approved.add(user)
        # 批准之后那个码就没用了：留着只会让旧消息永远有效。
        for code, owner in list(self._codes.items()):
            if owner == user:
                self._codes.pop(code, None)
        self._save()

    def ensure_code(self, user: str) -> str:
        """给这个人生成一个码（已经有就复用，别刷屏）。"""
        user = str(user)
        for code, owner in self._codes.items():
            if owner == user:
                return code
        code = "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LENGTH))
        self._codes[code] = user
        self._save()
        return code

    def approve_code(self, code: str) -> str:
        """按码批准。返回被批准的人；码不对返回空串。"""
        user = self._codes.pop(str(code).strip().upper(), "")
        if not user:
            return ""
        self._approved.add(user)
        self._save()
        return user

    @property
    def approved(self) -> tuple[str, ...]:
        return tuple(sorted(self._approved))

    @property
    def pending(self) -> tuple[tuple[str, str], ...]:
        return tuple((code, user) for code, user in sorted(self._codes.items()))
