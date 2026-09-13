"""spoolkit：面向本地小模型的编程特化 Agent。"""

from importlib.metadata import PackageNotFoundError, version

try:
    # 版本只有一处真相：pyproject.toml。装上了就报装的那个版本；
    # 源码树里直接跑（没装过）时退回开发版本号，而不是崩掉。
    __version__ = version("spoolkit")
except PackageNotFoundError:  # pragma: no cover - 只在未安装时走到
    __version__ = "0.0.0+dev"
