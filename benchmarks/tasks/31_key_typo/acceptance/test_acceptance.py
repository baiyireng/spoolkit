from user import name_of


def test_reads_name():
    assert name_of({"name": "Ann"}) == "Ann"
