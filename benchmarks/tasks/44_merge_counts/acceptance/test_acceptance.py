from counter import merge_counts


def test_sums_shared_keys():
    assert merge_counts({"x": 1}, {"x": 2, "y": 1}) == {"x": 3, "y": 1}


def test_empty():
    assert merge_counts({}, {"z": 4}) == {"z": 4}
