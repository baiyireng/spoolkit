from search import contains


def test_case_insensitive():
    assert contains("Hello World", "hello") is True


def test_absent_returns_false():
    assert contains("Hello", "zzz") is False
