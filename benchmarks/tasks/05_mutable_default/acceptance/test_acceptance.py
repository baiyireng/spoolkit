from bucket import add_item


def test_isolation():
    first = add_item("a")
    second = add_item("b")
    assert first == ["a"]
    assert second == ["b"]


def test_explicit_bucket():
    bucket = []
    assert add_item("x", bucket) == ["x"]
    assert bucket == ["x"]
