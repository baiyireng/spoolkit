from flag import is_positive


def test_positive():
    assert is_positive(3) is True


def test_negative():
    assert is_positive(-1) is False
