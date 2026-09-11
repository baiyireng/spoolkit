from length import length


def test_none_is_zero():
    assert length(None) == 0


def test_normal():
    assert length("abc") == 3
