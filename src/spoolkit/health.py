"""面向人的探活：模型通不通。不进模型上下文。

不启 llama-server，也不认某台机器的地址。地址来自这次运行已经生效的
`--provider` / `--base-url`（或用户配置）。页面每几秒打一次 `/state`，
所以探活带几秒缓存，不必每次都真去请求模型。

**网页壳上探活不在请求路径里**（`web/probe.py`）：那里有一条后台线程按
`CACHE_TTL` 同样的节拍探，`/state` 只读最近一次结果。这里的缓存于是成了
第二道（进程内其它调用者：桥、`spool status`、终端那一头）。
"""

from __future__ import annotations

import threading
import time
from typing import Sequence

import httpx

from spoolkit import settings
from spoolkit.llm.llamacpp import DEFAULT_BASE_URL, proxy_for

CACHE_TTL = 3.0

#: 一次探活的超时。1 秒的依据：本机 llama-server 起没起是**连接层**的问题，
#: 一秒内一定有答案（立刻连上 / 明确被拒 / 连不上）。等更久只是把这次探测拖长，
#: 而它要么长在请求路径上（网页壳改后台之前：实测切换卡 3.4 秒），要么占着
#: 后台线程——探活是取一次状态，不是等一个结果。
PROBE_TIMEOUT_SECONDS = 1.0

#: 探活结果的分类（`_result` 里那个**只增**的 `kind`）。为什么值得有：
#: "根本连不上"与"在、只是没在超时内回话"对人意味着不同的事，下一步也不同
#: （前者去起服务，后者去看模型是不是在加载）。`ok` / `detail` 的口径一个字没动。
KIND_CONNECT = "connect"
KIND_SLOW = "slow"
KIND_SERVER = "server"

_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}
_CACHE_LOCK = threading.Lock()


def _result(*, ok: bool, target: str, detail: str, kind: str = "") -> dict:
    return {
        "ok": ok,
        "name": "model",
        "target": target,
        "detail": detail,
        "kind": kind,
    }


def flag_from_extra(extra: Sequence[str], name: str) -> str:
    """从转发给子进程的参数里取出 `--name value`。"""
    flag = f"--{name}"
    items = list(extra)
    prefix = flag + "="
    for index, item in enumerate(items):
        if item == flag and index + 1 < len(items):
            return str(items[index + 1])
        if item.startswith(prefix):
            return item[len(prefix) :]
    return ""


def probe_model(
    *,
    provider: str = "",
    base_url: str = "",
    timeout: float = PROBE_TIMEOUT_SECONDS,
    cached: bool = True,
) -> dict:
    """探一次模型。失败返回 ok=False，不抛。"""
    provider = (provider or "").strip().lower() or "fake"
    target = (base_url or "").strip()
    key = (provider, target)
    if cached:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
            if hit and time.monotonic() - hit[0] < CACHE_TTL:
                return dict(hit[1])
    result = _probe_uncached(provider=provider, target=target, timeout=timeout)
    if cached:
        with _CACHE_LOCK:
            _CACHE[key] = (time.monotonic(), dict(result))
    return result


def _probe_uncached(*, provider: str, target: str, timeout: float) -> dict:
    if provider == "fake":
        return _result(ok=True, target="", detail="假模型（不连本机端口）")
    if provider != "llamacpp":
        label = "Gemini 云端" if provider == "gemini" else f"{provider}（云端）"
        return _result(ok=True, target=target, detail=f"{label}：不探本机端口")
    if not target:
        target = DEFAULT_BASE_URL
    try:
        proxy = proxy_for(target)
        with httpx.Client(
            base_url=target.rstrip("/"),
            timeout=timeout,
            proxy=proxy,
            trust_env=False,
        ) as client:
            response = client.get("/props")
    except httpx.ConnectTimeout as exc:
        # 连都没连上（SYN 没人应）：与"连不上"同一类，不是"太慢"。
        detail = str(exc).strip() or type(exc).__name__
        return _result(ok=False, target=target, detail=detail, kind=KIND_CONNECT)
    except httpx.TimeoutException as exc:
        # 连上了、只是没在超时内回话（模型在加载 / 太慢）：这一种要说成"慢"，
        # 因为人的下一步与"根本没有进程在听"不一样。
        detail = str(exc).strip() or type(exc).__name__
        return _result(ok=False, target=target, detail=detail, kind=KIND_SLOW)
    except Exception as exc:
        detail = str(exc).strip() or type(exc).__name__
        return _result(ok=False, target=target, detail=detail, kind=KIND_CONNECT)
    if response.status_code == 200:
        return _result(ok=True, target=target, detail="llama-server /props 通")
    return _result(
        ok=False,
        target=target,
        detail=f"HTTP {response.status_code}",
        kind=KIND_SERVER,
    )


def probe_from_extra(
    extra: Sequence[str],
    *,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> dict:
    """按网页壳转发给子进程的那组参数探活。没写就退回用户配置。"""
    provider = flag_from_extra(extra, "provider")
    base_url = flag_from_extra(extra, "base-url")
    if not provider:
        provider, _ = settings.resolve("provider", "")
    if not provider:
        provider = "fake"
    if not base_url:
        base_url, _ = settings.resolve("base_url", "")
    return probe_model(provider=provider, base_url=base_url, timeout=timeout)


def describe_change(old: dict | None, new: dict) -> str | None:
    """通↔断才说一句；状态没变就闭嘴。"""
    if old is not None and bool(old.get("ok")) == bool(new.get("ok")):
        return None
    target = str(new.get("target") or "").strip()
    where = f"（{target}）" if target else ""
    if new.get("ok"):
        return f"模型通{where}"
    detail = str(new.get("detail") or "连不上").strip()
    return f"模型不通{where}：{detail}"


def probe_spec(
    spec: str,
    *,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    cached: bool = True,
) -> tuple[bool, str, str]:
    """按配方上的探活地址打一次。http(s) GET，tcp 试连端口。失败不抛。"""
    spec = (spec or "").strip()
    key = ("spec", spec)
    if cached and spec:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
            if hit and time.monotonic() - hit[0] < CACHE_TTL:
                ok, target, detail = hit[1]
                return bool(ok), str(target), str(detail)
    result = _probe_spec_uncached(spec, timeout)
    if cached and spec:
        with _CACHE_LOCK:
            _CACHE[key] = (time.monotonic(), result)
    return result


def _probe_spec_uncached(spec: str, timeout: float) -> tuple[bool, str, str]:
    if not spec:
        return False, "", "no health URL"
    lowered = spec.lower()
    if lowered.startswith("tcp:"):
        return _probe_tcp(spec, timeout)
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return _probe_http(spec, timeout)
    return False, spec, "unsupported health (use http(s) or tcp:host:port)"


def _probe_tcp(spec: str, timeout: float) -> tuple[bool, str, str]:
    import socket

    body = spec.split(":", 1)[1]
    if body.startswith("//"):
        body = body[2:]
    if body.startswith("["):
        return False, spec, "IPv6 probe not supported"
    host, sep, port_s = body.rpartition(":")
    if not sep or not host or not port_s.isdigit():
        return False, spec, "tcp health must be tcp:host:port"
    port = int(port_s)
    target = f"{host}:{port}"
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, target, "port open"
    except OSError as exc:
        detail = str(exc).strip() or type(exc).__name__
        return False, target, detail


def _probe_http(url: str, timeout: float) -> tuple[bool, str, str]:
    try:
        proxy = proxy_for(url)
        with httpx.Client(timeout=timeout, proxy=proxy, trust_env=False) as client:
            response = client.get(url)
    except Exception as exc:
        detail = str(exc).strip() or type(exc).__name__
        return False, url, detail
    if 200 <= response.status_code < 300:
        return True, url, f"HTTP {response.status_code}"
    return False, url, f"HTTP {response.status_code}"


def probe_services() -> list[dict]:
    """本机 `[[service]]` 表的红绿。没写表就是空列表，不探隧道。

    隧道（常见名 `tunnel`）是可选的通道 SOCKS，不是内置依赖。
    """
    from spoolkit.services import probe_all

    return probe_all()


def describe_service_change(old: dict | None, new: dict) -> str | None:
    """外接配方通↔断时的那一行。"""
    if old is not None and bool(old.get("ok")) == bool(new.get("ok")):
        return None
    name = str(new.get("name") or "外接服务").strip() or "外接服务"
    target = str(new.get("target") or "").strip()
    where = f"（{target}）" if target else ""
    if new.get("ok"):
        return f"{name}通{where}"
    detail = str(new.get("detail") or "连不上").strip()
    return f"{name}不通{where}：{detail}"


def down_line(row: dict) -> str:
    """给最近聊天看的那一句。模型用「模型不通」，配方用自己的名字。"""
    name = str(row.get("name") or "")
    if name in {"", "model", "模型"}:
        return describe_change({"ok": True}, row) or "模型不通"
    return describe_service_change({"ok": True}, row) or f"{name}不通"


def collect_rows(extra_args: Sequence[str] = ()) -> list[dict]:
    """模型 + 本机配方，给断开通知用。"""
    rows = [probe_from_extra(extra_args)]
    try:
        rows.extend(probe_services())
    except Exception:
        pass
    return rows


class DisconnectWatch:
    """只抓通→断。默认关；开机已经断了的不算；冷却期内不刷屏。"""

    def __init__(self, *, enabled: bool = False, cooldown: float = 300.0) -> None:
        self.enabled = enabled
        self.cooldown = cooldown
        self._ok: dict[str, bool] = {}
        self._sent_at: dict[str, float] = {}

    def edges(self, rows: list[dict], now: float | None = None) -> list[dict]:
        stamp = time.monotonic() if now is None else now
        found: list[dict] = []
        if not self.enabled:
            return found
        for row in rows:
            name = str(row.get("name") or "")
            if not name:
                continue
            ok = bool(row.get("ok"))
            prev = self._ok.get(name)
            self._ok[name] = ok
            if prev is True and not ok:
                last = self._sent_at.get(name)
                if last is not None and stamp - last < self.cooldown:
                    continue
                self._sent_at[name] = stamp
                found.append(row)
        return found
