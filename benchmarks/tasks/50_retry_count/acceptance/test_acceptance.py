from retry import attempts


def test_counts_all():
    assert attempts(3) == 3


def test_zero():
    assert attempts(0) == 0
