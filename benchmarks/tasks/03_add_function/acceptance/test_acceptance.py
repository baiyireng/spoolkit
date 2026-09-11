from text import lower_first, upper_first


def test_lower_first():
    assert lower_first("Hello") == "hello"
    assert lower_first("") == ""


def test_upper_first_unchanged():
    assert upper_first("hello") == "Hello"
