"""给人看的探活：模型通不通。不进模型上下文。

不启 llama-server，也不认某台机器的地址。地址来自这次运行已经生效的
`--provider` / `--base-url`（或用户配置）。页面每几秒打一次 `/state`，
所以探活带几秒缓存，别每次都锤模型。
"""

from __future__ import annotations

import threading
import time
from typing import Sequence

import httpx

from spoolkit import settings
from spoolkit.llm.llamacpp import DEFAULT_BASE_URL, proxy_for

CACHE_TTL = 3.0

_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}
_CACHE_LOCK = threading.Lock()


def _result(*, ok: bool, target: str, detail: str) -> dict:
    return {
        "ok": ok,
        "name": "model",
        "target": target,
        "detail": detail,
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
    timeout: float = 1.0,
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
    except Exception as exc:
        detail = str(exc).strip() or type(exc).__name__
        return _result(ok=False, target=target, detail=detail)
    if response.status_code == 200:
        return _result(ok=True, target=target, detail="llama-server /props 通")
    return _result(
        ok=False,
        target=target,
        detail=f"HTTP {response.status_code}",
    )


def probe_from_extra(extra: Sequence[str], *, timeout: float = 1.0) -> dict:
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
    timeout: float = 1.0,
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
        return False, "", "没有探活地址"
    lowered = spec.lower()
    if lowered.startswith("tcp:"):
        return _probe_tcp(spec, timeout)
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return _probe_http(spec, timeout)
    return False, spec, "不认这种探活（只要 http(s) 或 tcp:host:port）"


def _probe_tcp(spec: str, timeout: float) -> tuple[bool, str, str]:
    import socket

    body = spec.split(":", 1)[1]
    if body.startswith("//"):
        body = body[2:]
    if body.startswith("["):
        return False, spec, "暂不探 IPv6"
    host, sep, port_s = body.rpartition(":")
    if not sep or not host or not port_s.isdigit():
        return False, spec, "tcp 地址要写成 tcp:host:port"
    port = int(port_s)
    target = f"{host}:{port}"
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, target, "端口开着"
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
