from conf import merge


def test_defaults_not_mutated():
    defaults = {"a": 1}
    merge(defaults, {"b": 2})
    assert defaults == {"a": 1}


def test_override_wins():
    assert merge({"a": 1}, {"a": 2}) == {"a": 2}


def test_extra_keys_kept():
    assert merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}
