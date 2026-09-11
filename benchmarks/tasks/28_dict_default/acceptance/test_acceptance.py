from config_lookup import timeout_of


def test_missing_uses_default():
    assert timeout_of({}) == 30


def test_explicit_wins():
    assert timeout_of({"timeout": 5}) == 5
