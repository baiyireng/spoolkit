"""上下文区段。

装配器把上下文切成若干区段分别核算配额。priority 越小越先被保留；
mandatory 为 True 的区段永不裁剪，超限时直接报错（说明配置本身不合理）。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Section:
    """上下文中的一个区段。"""

    name: str
    text: str
    priority: int
    mandatory: bool = False

