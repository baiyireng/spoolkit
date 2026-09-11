from listutil import first_three


def test_returns_three():
    assert first_three([1, 2, 3, 4]) == [1, 2, 3]


def test_short_input_is_safe():
    assert first_three([7]) == [7]
