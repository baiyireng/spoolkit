"""网络出口配置。

本项目默认走系统代理。理由很实际：不少环境直连会被地域或防火墙阻断，
而系统代理是用户已经配好的东西，agent 没有理由再要求用户配第二遍。
环境变量优先级高于系统设置。
"""

from urllib.request import getproxies

_KEYS = ("https", "http", "all")


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

