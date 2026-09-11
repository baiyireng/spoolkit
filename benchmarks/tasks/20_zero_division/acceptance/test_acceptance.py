from rate import ratio


def test_zero_denominator_returns_zero():
    assert ratio(5, 0) == 0.0


def test_normal_case_unchanged():
    assert ratio(1, 4) == 0.25
