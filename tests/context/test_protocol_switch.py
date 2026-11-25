"""protocol_v2 默认关闭。拨到 1 再拨回 0，旧 JSON 路径还在。"""

from spoolkit.agent.protocol import TurnProtocol
from spoolkit.context.phase_budget import protocol_v2_enabled
from spoolkit.limits import knob


class _EmptyRegistry:
    def names(self) -> tuple[str, ...]:
        return ()

    def get(self, name: str):
        return None


def test_开关默认关闭且拨回后旧协议仍带final() -> None:
    entry = knob("protocol_v2")
    assert entry is not None
    assert entry.default == 0
    assert entry.overridable is True
    assert protocol_v2_enabled() is False
    assert protocol_v2_enabled({"protocol_v2": 1}) is True
    assert protocol_v2_enabled({"protocol_v2": 0}) is False

    off = TurnProtocol(v2=protocol_v2_enabled())
    on = TurnProtocol(v2=protocol_v2_enabled({"protocol_v2": 1}))
    back = TurnProtocol(v2=protocol_v2_enabled({"protocol_v2": 0}))
    assert "final" in off.schema(_EmptyRegistry())["properties"]
    assert "final" not in on.schema(_EmptyRegistry())["properties"]
    assert "final" in back.schema(_EmptyRegistry())["properties"]
