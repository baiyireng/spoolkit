"""声明了 `requires-python >= 3.12`，就得真的能在 3.12 上导入。

这条测试的由来是一个真实事故：包在 3.12 上**有 18 个模块直接导不进来**
（`NameError: name 'PendingChanges' is not defined`）——签名里引用了稍后
才定义的类，而 Python 3.14 的惰性注解（PEP 649）把这个问题遮住了：
开发机跑 3.14，用户装 3.12，`requires-python` 写着的下限根本没被验证过。

所以这里逐个导入每个模块。在 3.14 上它几乎必然通过（惰性注解），
在 3.12/3.13 上它会抓到同一类错误——这才是它的价值。
"""

import importlib
import pkgutil
import sys

import agents_dev


def test_每个模块都能导入() -> None:
    failures: list[str] = []
    modules = list(pkgutil.walk_packages(agents_dev.__path__, "agents_dev."))
    for item in modules:
        try:
            importlib.import_module(item.name)
        except Exception as exc:  # noqa: BLE001 - 报出全部失败，不要只报第一个
            failures.append(f"{item.name}: {type(exc).__name__}: {exc}")
    assert not failures, (
        f"Python {sys.version.split()[0]} 上有 {len(failures)} 个模块导入失败：\n"
        + "\n".join(failures[:10])
    )
