from calc import add


def test_returns_value():
    assert add(2, 3) == 5


def test_returns_value_again():
    assert add(-1, 1) == 0
