from bounds import clamp


def test_too_large():
    assert clamp(15, 0, 10) == 10


def test_too_small():
    assert clamp(-3, 0, 10) == 0


def test_inside_unchanged():
    assert clamp(5, 0, 10) == 5
