import pytest

from validate import positive


def test_message_names_the_value():
    with pytest.raises(ValueError) as caught:
        positive(-3)
    assert "-3" in str(caught.value)


def test_still_accepts_positive():
    assert positive(2) == 2
