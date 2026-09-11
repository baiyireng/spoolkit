from paths import join_path


def test_adds_separator():
    assert join_path("src", "main.py") == "src/main.py"


def test_keeps_existing_separator():
    assert join_path("src/", "main.py") == "src/main.py"
