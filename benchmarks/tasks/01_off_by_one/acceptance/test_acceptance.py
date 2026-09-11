from calc import sum_to


def test_sum_to():
    assert sum_to(1) == 1
    assert sum_to(5) == 15
    assert sum_to(10) == 55
