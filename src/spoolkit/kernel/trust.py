"""放行：谁能驱动、能碰哪几个工作区。

为什么从工作区级搬到身份级：原先放行记录写在 `.agent/bridge-pairings.json`——
那是**工作区里**的文件，于是"信任"和"地点"绑死：换工作区就得重新配对。而人还是
同一个人。

一条放行 = (通道, 用户, 能碰的工作区列表)。通道必须进主键：QQ 的 openid 与
Telegram 的 user id 不是一套命名空间，同名会串。

旧文件不删也不改：`adopt_legacy` 只是把它读进来，什么时候真的不要了由人决定。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path

from spoolkit.bridge.pairing import Pairings
from spoolkit.kernel import jsonstore
from spoolkit.kernel.workspaces import state_dir

TRUST = "channel-trust.json"
LEGACY_NAME = "bridge-pairings.json"

# 配对码的字母表去掉 0/O、1/I/L：这串码要靠人在聊天里念、在终端里敲。
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LENGTH = 6


@dataclass(frozen=True)
class Grant:
    channel: str
    user: str
    roots: tuple[Path, ...]
    label: str = ""
    source: str = "manual"  # manual / legacy


@dataclass(frozen=True)
class PendingCode:
    code: str
    user: str
    channel: str
    root: Path


def trust_path() -> Path:
    return state_dir() / TRUST


def _load() -> dict:
    payload = jsonstore.read_json(trust_path(), {})
    return payload if isinstance(payload, dict) else {}


def _grant_key(channel: str, user: str) -> str:
    # 通道与用户都可能带冒号（openid 见过），所以用不可见分隔符拼一个稳定键。
    return f"{channel}\u0000{user}"


def _mapping(value: object) -> dict:
    """从一个可能坏掉的容器里取出可用的 dict。

    `channel-trust.json` 是人也能手改的机器级文件，形状坏掉（`grants` 写成数组之类）
    时按"空的"处理——放行有问题应该是**够不到**，不是整个通道起不来。读路径与写路径
    都要走这里：写路径上 `dict(坏形状)` 会抛，等于"文件坏了就再也修不回来"。
    """
    return dict(value) if isinstance(value, dict) else {}


def _usable(channel: object, user: object) -> bool:
    """这条放行/这个码有没有意义：通道和用户都得有字。

    旧配对文件是别人写的，里面可能留着空用户名（`ensure_code("")` 写进去过）。
    空用户名的条目**不入账**，但也**不能让认领路径抛异常**——桥是在启动时认领的，
    一个垃圾条目不该把桥拦在门外。
    """
    return bool(str(channel).strip()) and bool(str(user).strip())


def _root_list(value: object) -> tuple[Path, ...]:
    """`roots` 只认"一串路径"。

    字符串会被逐字符拆成 `D` / `:` / `/` / `t` 这种单字符路径，造出
    "`is_allowed` 说能、`may_use` 说不能"的幻影放行，所以显式拒掉非序列形状。
    """
    if not isinstance(value, (list, tuple, set)):
        return ()
    return tuple(
        Path(str(text)).resolve() for text in value if str(text).strip()
    )


def list_grants() -> list[Grant]:
    """坏条目跳过：放行有问题应该是"够不到"，不是"整个通道起不来"。"""
    data = _load()
    found: list[Grant] = []
    for key, item in _mapping(data.get("grants")).items():
        if not isinstance(item, dict):
            continue
        channel, _, user = str(key).partition("\u0000")
        roots = _root_list(item.get("roots"))
        if not roots:
            continue
        found.append(
            Grant(
                channel=channel,
                user=user,
                roots=roots,
                label=str(item.get("label") or ""),
                source=str(item.get("source") or "manual"),
            )
        )
    return found


def grant(
    channel: str, user: str, roots: list[Path | str] | tuple[Path | str, ...], label: str = ""
) -> None:
    """放行这个人到这几个工作区。`roots` 必须显式给非空值。"""
    channel = str(channel).strip()
    user = str(user).strip()
    picked = tuple(
        dict.fromkeys(Path(str(item)).resolve() for item in roots if str(item).strip())
    )
    if not channel or not user:
        raise ValueError("放行要给通道和用户")
    if not picked:
        raise ValueError(
            "放行必须点名工作区——不给就等于「谁都能碰这台机器上的一切」，"
            "那是这个通道最容易出的事。"
        )

    def mutate(payload: object) -> dict:
        data = dict(payload) if isinstance(payload, dict) else {}
        grants = _mapping(data.get("grants"))
        # 被合并的那一条自己也可能被手改坏：不是 dict 就当作没有；`roots` 同样只认
        # 一串路径。两处都必须收——`grant` 是 `adopt_legacy`（桥启动）与
        # `approve_code`（人手批准）的必经之路，坏形状在这里抛等于那两条路照样被打死；
        # 而字符串 `roots` 会被**逐字符写回放行表**，凭空多出 `D:\` 这种根。
        existing = grants.get(_grant_key(channel, user))
        existing = existing if isinstance(existing, dict) else {}
        previous = [str(item) for item in _root_list(existing.get("roots"))]
        merged = list(dict.fromkeys(previous + [str(item) for item in picked]))
        grants[_grant_key(channel, user)] = {
            "channel": channel,
            "user": user,
            "roots": merged,
            "label": label or str(existing.get("label") or ""),
            "source": "manual",
        }
        return {**data, "grants": grants}

    jsonstore.update_json(trust_path(), mutate, {})
    _drop_codes_for(channel, user)


def _drop_codes_for(channel: str, user: str) -> None:
    """批准之后那个码就没用了：留着只会让旧消息永远有效。"""
    def mutate(payload: object) -> dict:
        data = dict(payload) if isinstance(payload, dict) else {}
        codes = {
            code: item
            for code, item in _mapping(data.get("codes")).items()
            if not (
                isinstance(item, dict)
                and str(item.get("channel")) == channel
                and str(item.get("user")) == user
            )
        }
        return {**data, "codes": codes}

    jsonstore.update_json(trust_path(), mutate, {})


def revoke(channel: str, user: str) -> bool:
    """撤销放行。返回这次是不是真的撤掉了。"""
    removed = False

    def mutate(payload: object) -> dict:
        nonlocal removed
        data = dict(payload) if isinstance(payload, dict) else {}
        grants = _mapping(data.get("grants"))
        removed = grants.pop(_grant_key(str(channel), str(user)), None) is not None
        return {**data, "grants": grants}

    jsonstore.update_json(trust_path(), mutate, {})
    _drop_codes_for(str(channel), str(user))
    return removed


def is_allowed(channel: str, user: str) -> bool:
    return any(
        item.channel == str(channel) and item.user == str(user) for item in list_grants()
    )


def allowed_roots(channel: str, user: str) -> tuple[Path, ...]:
    for item in list_grants():
        if item.channel == str(channel) and item.user == str(user):
            return item.roots
    return ()


def may_use(channel: str, user: str, root: Path | str) -> bool:
    """这个人被放行到**这个**工作区了吗。没有就够不到，且要说清为什么。"""
    target = Path(root).resolve()
    return target in allowed_roots(channel, user)


def _codes() -> dict[str, dict]:
    data = _load()
    raw = _mapping(data.get("codes"))
    return {
        str(code).upper(): dict(item)
        for code, item in raw.items()
        if isinstance(item, dict)
    }


def pending() -> list[PendingCode]:
    found: list[PendingCode] = []
    for code, item in sorted(_codes().items()):
        root_text = str(item.get("root") or "").strip()
        if not root_text:
            continue
        found.append(
            PendingCode(
                code=code,
                user=str(item.get("user") or ""),
                channel=str(item.get("channel") or ""),
                root=Path(root_text).resolve(),
            )
        )
    return found


def ensure_code(channel: str, user: str, root: Path | str) -> str:
    """给这个人生成一个码（已经有就复用，别刷屏）。"""
    channel, user = str(channel), str(user)
    target = str(Path(root).resolve())
    for item in pending():
        if item.channel == channel and item.user == user and str(item.root) == target:
            return item.code
    code = "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LENGTH))

    def mutate(payload: object) -> dict:
        data = dict(payload) if isinstance(payload, dict) else {}
        codes = _mapping(data.get("codes"))
        codes[code] = {"user": user, "channel": channel, "root": target}
        return {**data, "codes": codes}

    jsonstore.update_json(trust_path(), mutate, {})
    return code


def forget_code(code: str) -> bool:
    """丢掉一个还没批准的码（不给理由，也不放行谁）。"""
    dropped = False

    def mutate(payload: object) -> dict:
        nonlocal dropped
        data = dict(payload) if isinstance(payload, dict) else {}
        codes = _mapping(data.get("codes"))
        dropped = codes.pop(str(code).strip().upper(), None) is not None
        return {**data, "codes": codes}

    jsonstore.update_json(trust_path(), mutate, {})
    return dropped


def find_code(code: str) -> PendingCode | None:
    """先找机器级的，再找旧文件里的（认领过之后旧码也能批）。"""
    wanted = str(code).strip().upper()
    for item in pending():
        if item.code == wanted:
            return item
    for item in _legacy_pending():
        if item.code == wanted:
            return item
    return None


def approve_code(code: str) -> PendingCode | None:
    got = find_code(code)
    if got is None:
        return None
    if not _usable(got.channel, got.user):
        # 旧文件里可能留着空用户名的条目（`Pairings` 不校验条目内容）。放行一个
        # "没有名字的人"没有意义，但**不能抛**——批准是人手敲的一条路径，
        # 报"没这个码"远比崩掉好。条目留在原地，不动它。
        return None
    grant(got.channel, got.user, [got.root])
    forget_code(code)
    _approve_in_legacy(got)
    return got


def _legacy_pairings(root: Path) -> Pairings | None:
    path = root / ".agent" / LEGACY_NAME
    if not path.is_file():
        return None
    return Pairings(path)


def _legacy_pending() -> list[PendingCode]:
    """旧文件里的码：没有通道维度，所以只能标成"(旧)"，批准时按调用方给的通道算。"""
    from spoolkit.kernel.workspaces import known

    found: list[PendingCode] = []
    for root in known():
        pairings = _legacy_pairings(root)
        if pairings is None:
            continue
        for code, user in pairings.pending:
            found.append(PendingCode(code=code, user=user, channel="", root=root))
    return found


def _approve_in_legacy(target: PendingCode) -> None:
    """旧文件里也有这个码的话，一并标成已批准（原文件不改结构、也不删）。"""
    pairings = _legacy_pairings(target.root)
    if pairings is None:
        return
    if pairings.approve_code(target.code):
        return
    pairings.approve(target.user)


def _import_legacy_codes(channel: str, root: Path, codes: tuple[tuple[str, str], ...]) -> None:
    """把旧文件里**还没批准**的码也读进机器级（记上它请求的工作区与通道）。

    设计文档 §6.1 的原话是"待批准的码一并读进机器级（key 带上它请求的工作区）"。
    不读进来会怎样：桥认领过之后，人手上那条通道刚发给他的码
    `spool approve <码>` 会说不认识——`find_code` 往旧文件兜底的那条路只扫
    **已登记**的工作区，而通道服务的未必在登记表里。
    用 `setdefault`：同一个码已经存在时不抢，认领因此是幂等的。
    """
    if not codes:
        return

    def mutate(payload: object) -> dict:
        data = dict(payload) if isinstance(payload, dict) else {}
        stored = _mapping(data.get("codes"))
        for code, user in codes:
            if not _usable(channel, user):
                continue
            stored.setdefault(
                str(code).strip().upper(),
                {"user": str(user), "channel": channel, "root": str(root)},
            )
        return {**data, "codes": stored}

    jsonstore.update_json(trust_path(), mutate, {})


def adopt_legacy(root: Path | str, channel: str) -> int:
    """把旧配对文件里已批准的人认领成"这条通道 + 这个工作区"的放行。

    由通道自己调用（旧文件没有通道维度，只有当前那条通道知道该认成谁的）。
    幂等；返回现在一共放行了多少条（含以前认领的）。
    """
    resolved = Path(root).resolve()
    pairings = _legacy_pairings(resolved)
    if pairings is None:
        return 0
    channel = str(channel)
    for user in pairings.approved:
        if not _usable(channel, user):
            # 空用户名的条目跳过：**认领是在桥启动时跑的**，一个垃圾条目不该
            # 把桥拦在门外（`Pairings` 的契约是"读不了就当空的，配对是增强"）。
            continue
        grant(channel, user, [resolved], label="从旧配对记录认领")
    _import_legacy_codes(channel, resolved, pairings.pending)
    return sum(1 for item in list_grants() if item.channel == channel)
