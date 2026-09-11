import inspect

from geometry import area


def test_area_still_works():
    assert area(3, 4) == 12


def test_annotations_present():
    hints = inspect.signature(area).parameters
    assert hints["width"].annotation is int
    assert hints["height"].annotation is int
    assert inspect.signature(area).return_annotation is int
