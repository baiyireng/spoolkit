from text import clean


def test_strips_and_lowers():
    assert clean("  Hi  ") == "hi"


def test_plain():
    assert clean("abc") == "abc"
