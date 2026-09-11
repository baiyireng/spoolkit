from joinutil import join


def test_numbers():
    assert join([1, 2, 3]) == "1, 2, 3"


def test_mixed():
    assert join(["a", 1]) == "a, 1"


def test_strings_unchanged():
    assert join(["x", "y"]) == "x, y"
