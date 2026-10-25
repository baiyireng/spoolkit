"""网络出口配置。

本项目默认走系统代理。理由很实际：不少环境直连会被地域或防火墙阻断，
而系统代理是用户已经配好的东西，agent 没有理由再要求用户配第二遍。
环境变量优先级高于系统设置。
"""

from urllib.request import getproxies

_KEYS = ("https", "http", "all")

# httpx 0.28.x 在**构造 Client**（不是发请求）时就会把 NO_PROXY 按逗号切开逐个
# `URLPattern(...)` 解析，`[::1]` 这种带方括号的 IPv6 写法会被它当成 URL 去解
# 端口，直接抛 `httpx.InvalidURL: Invalid port: ':1]'`。于是只要环境里有人写了
# `NO_PROXY=...,[::1]`，**任何**默认 trust_env=True 的 httpx 客户端都构造不出来
# ——连本机 8080 都碰不到。方括号是 URL 里的写法，不是主机名写法。
# 下面 `client_proxy` 就是这道防御的统一出口：我们自己决定代理，然后关掉 httpx
# 的环境嗅探（`trust_env=False`）。
def client_proxy(
    explicit: str | None = None, transport: object | None = None
) -> str | None:
    """给出交给 httpx 的 `proxy=` 取值；**配套 `trust_env=False` 使用**。

    这个函数存在的唯一理由，是别再让 httpx 自己去读环境变量。httpx 0.28.x 在
    **构造 Client**（不是发请求）时就会把 NO_PROXY 按逗号切开逐个
    `URLPattern(...)` 解析，`[::1]` 这种带方括号的 IPv6 写法会被当成 URL 去解
    端口，当场抛 `httpx.InvalidURL: Invalid port: ':1]'`——连本机回环都碰不到。
    而 NO_PROXY 不归我们管（上层工具、别人的脚本都可能写），所以正确做法是：
    **代理由我们自己决定**，然后关掉 httpx 的环境嗅探。

    取值口径与 httpx 原本的 trust_env 行为一致，只是把来源换成我们能看住的地方：
    显式给的优先，没给就按用户已经配好的环境/系统代理补上（`system_proxy()`）。

    给了 `transport` 就返回 None：httpx 不允许同时指定 transport 与 proxy
    （`transport` 是测试与自建出口用的，那种场合代理本来就无意义）。
    """
    if transport is not None:
        return None
    return explicit or system_proxy()


def system_proxy(raw: dict[str, str] | None = None) -> str | None:
    """返回应当使用的代理地址，没有则返回 None。

    raw 仅用于测试注入；正常调用会读取环境变量与
    （在 Windows 上）注册表里的 Internet Settings。
    """
    proxies = getproxies() if raw is None else raw
    for key in _KEYS:
        value = proxies.get(key)
        if value:
            return value if "://" in value else f"http://{value}"
    return None

