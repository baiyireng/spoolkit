from spoolkit.agents.plan import narrow_scope


def test_范围内的提议被接受() -> None:
    allowed, rejected = narrow_scope(("scratch_lab/",), ("scratch_lab/**",))
    assert allowed == ("scratch_lab/**",)
    assert rejected == ()


def test_更窄的提议被接受() -> None:
    allowed, _ = narrow_scope(("scratch_lab/",), ("scratch_lab/calc.py",))
    assert allowed == ("scratch_lab/calc.py",)


def test_越界的提议被拒绝() -> None:
    allowed, rejected = narrow_scope(("scratch_lab/",), ("src/",))
    assert allowed == ()
    assert rejected == ("src/",)


def test_同名前缀不同目录会被拒绝() -> None:
    # "scratch_lab_other/" 不该被 "scratch_lab/" 放行
    allowed, rejected = narrow_scope(("scratch_lab/",), ("scratch_lab_other/",))
    assert allowed == ()
    assert rejected == ("scratch_lab_other/",)


def test_没有授权时全部拒绝() -> None:
    allowed, rejected = narrow_scope((), ("**",))
    assert allowed == ()
    assert rejected == ("**",)


def test_多个提议部分通过() -> None:
    allowed, rejected = narrow_scope(
        ("scratch_lab/", "tests/"), ("scratch_lab/**", "src/**", "tests/**")
    )
    assert allowed == ("scratch_lab/**", "tests/**")
    assert rejected == ("src/**",)


def test_收窄只会变小不会变大() -> None:
    granted = ("scratch_lab/",)
    allowed, _ = narrow_scope(granted, ("**",))
    assert allowed == ()


def test_显式授权整个项目时才放行全局提议() -> None:
    allowed, _ = narrow_scope(("**",), ("src/**",))
    assert allowed == ("src/**",)


def test_提议全部被驳回时可回退到授权范围() -> None:
    """回退到授权的语义：提议无效不等于整步变只读。"""
    granted = ("scratch_lab/",)
    effective, rejected = narrow_scope(granted, ("tests/",))
    assert effective == ()
    assert rejected == ("tests/",)
    # 调用方的回退规则
    assert (effective or granted) == granted
