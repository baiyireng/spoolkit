from money import total


def test_two_decimals():
    assert total(0.1, 0.2) == 0.3


def test_normal():
    assert total(1.25, 2.25) == 3.5
