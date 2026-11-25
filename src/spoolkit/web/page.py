"""网页壳的资源入口。

标记、样式、脚本在 ``assets/`` 里。这里只把它们读出来：

- ``INDEX`` 是浏览器打开的首页（样式和脚本走 ``/assets``）；
- ``HTML`` 是同一份标记拼上内联样式和经典脚本，给还在读字符串的测试和嵌入方。
  它在第一次被读取时才拼，服务端只引用 ``INDEX`` 时不付这份成本。

拼装在 ``static.py``。这个文件不再手写样式或脚本。执行顺序只认 ``static.JS_ORDER``。
"""

from spoolkit.web.static import index_html

INDEX = index_html()
_html: str | None = None


def __getattr__(name: str) -> str:
    if name != "HTML":
        raise AttributeError(name)
    global _html
    if _html is None:
        from spoolkit.web.static import compat_html
        _html = compat_html()
    return _html
