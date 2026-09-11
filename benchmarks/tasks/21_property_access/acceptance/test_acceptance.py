from user import User


def test_full_name_is_a_property():
    user = User("Ann", "Lee")
    assert user.full_name == "Ann Lee"


def test_parts_are_kept():
    user = User("Bo", "Wang")
    assert (user.first, user.last) == ("Bo", "Wang")
