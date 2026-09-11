from parse import to_int


def test_valid():
    assert to_int("42") == 42


def test_invalid_returns_none():
    assert to_int("abc") is None
    assert to_int("") is None
