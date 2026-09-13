"""抓取一层：出口、上限、以及"不许抓什么"。

三件事都不许写死，按项目一贯的规矩由调用方给：**走哪个代理、允许哪些域名、
多大的上限**。这里只提供默认值与判定。

有一条必须硬性的：**不许抓内网地址**。这个工具是给一个会写文件、会跑命令的
agent 用的，放它去抓 `http://127.0.0.1:8080`（本机的 llama-server）、
`http://169.254.169.254`（云主机元数据）等于把内网探测送上门。所以默认拦掉
环回/私有/链路本地/保留地址——要放开得显式写在 allow 里。
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from spoolkit.net import system_proxy

ALLOWED_SCHEMES = ("http", "https")
USER_AGENT = "spoolkit/0.0.1 (+https://github.com/baiyireng/spoolkit)"


@dataclass(frozen=True)
class WebPolicy:
    """联网取用的边界。空 allow = 允许任何**公网**域名（内网仍然拦）。"""

    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    proxy: str | None = None
    max_bytes: int = 2_000_000
    timeout: float = 20.0
    chunk_chars: int = 2400


@dataclass(frozen=True)
class Fetched:
    url: str
    status: int
    content_type: str
    body: bytes

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class WebError(RuntimeError):
    """抓取被拒或失败。给人看的话要说清是**哪一条**规矩挡的。"""


def _match(host: str, suffixes: tuple[str, ...]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == item or host.endswith("." + item) for item in suffixes)


def _is_public(host: str) -> bool:
    """解析出来的地址是否全是公网地址。解析失败按"不公网"处理。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        raw = info[4][0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            return False
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            return False
    return bool(infos)


def check(url: str, policy: WebPolicy) -> str:
    """要不要抓这个 URL。返回原 URL，或抛出 `WebError`。"""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise WebError(f"只支持 http/https，收到的 scheme 是 {parsed.scheme or '（空）'}")
    host = parsed.hostname or ""
    if not host:
        raise WebError("这个 URL 里没有主机名")
    if policy.deny and _match(host, policy.deny):
        raise WebError(f"{host} 在黑名单里（--web-deny）")
    if policy.allow and not _match(host, policy.allow):
        raise WebError(
            f"{host} 不在白名单里（--web-allow）。当前允许：{'、'.join(policy.allow)}"
        )
    if not _is_public(host):
        raise WebError(
            f"{host} 解析到内网/环回/保留地址，默认不抓——这个工具是给会写文件、"
            "会跑命令的 agent 用的，不能让它顺手探测内网。要放开得显式加白名单。"
        )
    return url.strip()


def fetch_text(url: str, policy: WebPolicy) -> Fetched:
    """真的去抓。大小上限按**读到的字节数**算，不是 Content-Length（那可以撒谎）。"""
    target = check(url, policy)
    proxy = policy.proxy if policy.proxy is not None else system_proxy()
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    try:
        with httpx.Client(
            timeout=policy.timeout, follow_redirects=True, proxy=proxy, headers=headers
        ) as client:
            with client.stream("GET", target) as response:
                body = b""
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > policy.max_bytes:
                        raise WebError(
                            f"页面超过 {policy.max_bytes // 1000} KB 上限，已中止。"
                            "（要看这么大的东西，先让它下载到工作区再用 read_file 分段读）"
                        )
                return Fetched(
                    url=str(response.url),
                    status=response.status_code,
                    content_type=response.headers.get("content-type", ""),
                    body=body,
                )
    except WebError:
        raise
    except httpx.HTTPError as exc:
        raise WebError(f"抓取失败（{type(exc).__name__}）：{exc}") from exc
