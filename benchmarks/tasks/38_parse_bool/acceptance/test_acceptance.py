from flags import to_bool


def test_false_string():
    assert to_bool("false") is False


def test_zero_string():
    assert to_bool("0") is False


def test_truthy_string():
    assert to_bool("true") is True
