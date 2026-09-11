from format_util import label, describe


def test_name_comes_first():
    assert describe({"name": "x", "value": 5}) == "x=5"


def test_label_itself_unchanged():
    assert label("a", 1) == "a=1"
