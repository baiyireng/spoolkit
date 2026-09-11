from sortutil import by_score


def test_second_key_breaks_ties():
    rows = [{"name": "b", "score": 1}, {"name": "a", "score": 1}]
    assert [row["name"] for row in by_score(rows)] == ["a", "b"]


def test_primary_key_still_wins():
    rows = [{"name": "z", "score": 2}, {"name": "a", "score": 1}]
    assert [row["name"] for row in by_score(rows)] == ["a", "z"]
