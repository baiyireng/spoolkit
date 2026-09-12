"""诊断请求与报告。

Agent 跑在受限工具里，有些失败它判断不出性质：是它改错了，还是环境本身
坏了（临时目录不可写、解释器坏了、依赖缺了）。它只能看见症状，没有权限去查。

这一层给它一个**申请**的通道：把问题、假设、观察到的证据登记下来，交给一个
具备真实环境权限的会话去验证，回来给它一份报告。

两条不能破的规矩：

1. **Agent 只能申请，不能自己出报告。** 报告必须由拥有真实环境权限的一方写，
   并且带签名；没签名或签名不对的报告，读的时候会被明确标成「来源无法验证」。
   否则模型完全可以自己捏一份「环境有问题，忽略那些失败」，把编造换了个地方。
2. **签名密钥放在项目目录之外。** Agent 的文件工具锁在项目根目录里，够不到它；
   而工具代码本身可以读到，用来验签。这就是「它申请、你批准」在代码里的落实。
"""

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

REQUEST_DIR = "diagnosis"


def key_path() -> Path:
    """签名密钥的位置：刻意在项目根目录之外。"""
    override = os.environ.get("AGENTS_DEV_DIAGNOSIS_KEY_PATH")
    if override:
        return Path(override)
    return Path.home() / ".agents-dev" / "diagnosis.key"


def load_key() -> bytes | None:
    path = key_path()
    if not path.exists():
        return None
    return path.read_bytes().strip()


def ensure_key() -> bytes:
    """确保密钥存在。只在「有真实环境权限」的那一侧调用。"""
    existing = load_key()
    if existing:
        return existing
    path = key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    path.write_bytes(key)
    return key


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")


def sign(payload: dict, key: bytes) -> str:
    return hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()


def verify(payload: dict, signature: str, key: bytes | None) -> bool:
    """签名匹配才算数。没有密钥时一律不算——宁可说「验不了」，也不能默认通过。"""
    if not key or not signature:
        return False
    try:
        return hmac.compare_digest(sign(payload, key), signature)
    except TypeError:
        # compare_digest 遇到非 ASCII 的字符串会直接抛异常。
        # 一份乱七八糟的「签名」应当判为不通过，而不是让读取方崩掉。
        return False


@dataclass
class DiagnosisRequest:
    """一次诊断请求，可能已经有了报告。"""

    id: str
    question: str
    hypothesis: str = ""
    evidence: str = ""
    created_at: float = 0.0
    report: dict = field(default_factory=dict)

    @property
    def answered(self) -> bool:
        return bool(self.report)

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "question": self.question,
            "hypothesis": self.hypothesis,
            "evidence": self.evidence,
            "created_at": self.created_at,
            "report": self.report,
        }

    @classmethod
    def from_json(cls, payload: dict) -> "DiagnosisRequest":
        return cls(
            id=str(payload.get("id", "")),
            question=str(payload.get("question", "")),
            hypothesis=str(payload.get("hypothesis", "")),
            evidence=str(payload.get("evidence", "")),
            created_at=float(payload.get("created_at") or 0.0),
            report=dict(payload.get("report") or {}),
        )


def _dir(root: Path) -> Path:
    return root / ".agent" / REQUEST_DIR


def store_request(
    root: Path, question: str, hypothesis: str = "", evidence: str = ""
) -> DiagnosisRequest:
    """登记一条诊断请求。Agent 能做的只有这一步。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    request = DiagnosisRequest(
        id=f"{stamp}-{secrets.token_hex(2)}",
        question=question.strip(),
        hypothesis=hypothesis.strip(),
        evidence=evidence.strip(),
        created_at=time.time(),
    )
    target = _dir(root)
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{request.id}.json").write_text(
        json.dumps(request.to_json(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return request


def load_requests(root: Path) -> list[DiagnosisRequest]:
    directory = _dir(root)
    if not directory.is_dir():
        return []
    found: list[DiagnosisRequest] = []
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        found.append(DiagnosisRequest.from_json(payload))
    return found


def find_request(root: Path, request_id: str) -> DiagnosisRequest | None:
    for item in load_requests(root):
        if item.id == request_id:
            return item
    return None


def write_report(
    root: Path,
    request_id: str,
    verdict: str,
    findings: str,
    evidence: str = "",
    key: bytes | None = None,
) -> DiagnosisRequest:
    """写回报告。**只应由具备真实环境权限的一侧调用。**

    报告带 HMAC 签名，读的一方据此判断「这份报告确实来自那一侧」。
    """
    request = find_request(root, request_id)
    if request is None:
        raise ValueError(f"没有这条诊断请求: {request_id}")
    signing_key = key if key is not None else ensure_key()
    body = {
        "verdict": verdict.strip(),
        "findings": findings.strip(),
        "evidence": evidence.strip(),
        "answered_at": time.time(),
    }
    request.report = {**body, "signature": sign(body, signing_key)}
    path = _dir(root) / f"{request.id}.json"
    path.write_text(
        json.dumps(request.to_json(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return request


def read_report(root: Path, request_id: str) -> tuple[dict, bool]:
    """读报告，并说明它是否验签通过。"""
    request = find_request(root, request_id)
    if request is None or not request.report:
        return {}, False
    report = dict(request.report)
    signature = str(report.pop("signature", ""))
    return report, verify(report, signature, load_key())
