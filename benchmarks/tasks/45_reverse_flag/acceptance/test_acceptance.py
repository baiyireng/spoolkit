from ranking import by_points


def test_descending():
    players = [{"name": "a", "points": 1}, {"name": "b", "points": 9}]
    assert [p["name"] for p in by_points(players, True)] == ["b", "a"]


def test_ascending():
    players = [{"name": "a", "points": 1}, {"name": "b", "points": 9}]
    assert [p["name"] for p in by_points(players, False)] == ["a", "b"]
