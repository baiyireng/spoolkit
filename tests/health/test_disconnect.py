"""通→断才通知；默认关，避免没人看着手机也被刷屏。"""

from spoolkit.health import DisconnectWatch, down_line


def _row(name: str, ok: bool, target: str = "http://x") -> dict:
    return {"name": name, "ok": ok, "target": target, "detail": "拒绝连接"}


def test_默认关什么都不报() -> None:
    watch = DisconnectWatch()
    assert watch.enabled is False
    assert watch.edges([_row("model", True)], now=1) == []
    assert watch.edges([_row("model", False)], now=2) == []


def test_首次采样已经断了也不报() -> None:
    """开机时模型就挂着，不是"刚刚断开"。"""
    watch = DisconnectWatch(enabled=True, cooldown=0)
    assert watch.edges([_row("model", False)], now=1) == []


def test_通变断才报一次() -> None:
    watch = DisconnectWatch(enabled=True, cooldown=60)
    assert watch.edges([_row("model", True)], now=1) == []
    down = watch.edges([_row("model", False)], now=2)
    assert len(down) == 1
    assert down[0]["name"] == "model"
    assert watch.edges([_row("model", False)], now=3) == []
    # 冷却期内又通又断：不刷屏
    assert watch.edges([_row("model", True)], now=10) == []
    assert watch.edges([_row("model", False)], now=11) == []
    # 冷却过后的通→断再报
    assert watch.edges([_row("model", True)], now=70) == []
    fired = watch.edges([_row("model", False)], now=71)
    assert fired


def test_断线文案给人看() -> None:
    text = down_line(
        {
            "name": "model",
            "ok": False,
            "target": "http://127.0.0.1:8080",
            "detail": "timed out",
        }
    )
    assert "不通" in text
    assert "8080" in text
    assert "timed out" in text
