from registry import Registry


def test_caller_cannot_pollute():
    registry = Registry()
    registry.add("a")
    borrowed = registry.items()
    borrowed.append("b")
    assert registry.items() == ["a"]


def test_still_lists_items():
    registry = Registry()
    registry.add("x")
    assert registry.items() == ["x"]
