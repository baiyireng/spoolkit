from compare import gap


def test_gap_is_never_negative():
    assert gap(1, 5) == 4


def test_gap_forward():
    assert gap(5, 1) == 4
