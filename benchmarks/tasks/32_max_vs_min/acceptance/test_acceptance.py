from stats import largest


def test_finds_largest():
    assert largest([1, 5, 3]) == 5


def test_single():
    assert largest([7]) == 7
