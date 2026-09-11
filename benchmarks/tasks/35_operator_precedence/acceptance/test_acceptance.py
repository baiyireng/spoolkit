from average import average


def test_two_numbers():
    assert average(2, 4) == 3


def test_negatives():
    assert average(-2, 2) == 0
