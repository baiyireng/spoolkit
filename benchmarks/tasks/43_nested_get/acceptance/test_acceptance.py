from nested import setting


def test_missing_section_returns_none():
    assert setting({}, "db", "host") is None


def test_missing_key_returns_none():
    assert setting({"db": {}}, "db", "host") is None


def test_present():
    assert setting({"db": {"host": "x"}}, "db", "host") == "x"
