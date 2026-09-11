from textsearch import find_all


def test_dot_is_literal():
    assert find_all("axb", "a.b") == []


def test_real_match():
    assert find_all("a.b a.b", "a.b") == ["a.b", "a.b"]
