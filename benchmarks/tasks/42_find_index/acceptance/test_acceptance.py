from lookup import index_of


def test_missing_returns_minus_one():
    assert index_of(["a", "b"], "z") == -1


def test_found():
    assert index_of(["a", "b"], "b") == 1
