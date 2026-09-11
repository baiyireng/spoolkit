from box import add_item


def test_default_is_fresh_each_call():
    assert add_item("a") == ["a"]
    assert add_item("b") == ["b"]


def test_explicit_box_still_appends():
    mine = ["x"]
    assert add_item("y", mine) == ["x", "y"]
