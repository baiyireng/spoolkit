from range_util import inclusive_range


def test_includes_both_ends():
    assert inclusive_range(1, 3) == [1, 2, 3]


def test_single_point():
    assert inclusive_range(5, 5) == [5]
