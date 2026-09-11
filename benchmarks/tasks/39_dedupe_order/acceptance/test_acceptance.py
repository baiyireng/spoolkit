from dedupe import unique


def test_keeps_first_occurrence_order():
    assert unique(["b", "a", "b", "c", "a"]) == ["b", "a", "c"]
