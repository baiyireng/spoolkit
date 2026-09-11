import pathlib

from pipeline import run
from util import clean_text


def test_new_name_works():
    assert clean_text("  Hi  ") == "hi"
    assert run("  Hi  ") == "hi"


def test_old_name_gone():
    source = pathlib.Path("util.py").read_text(encoding="utf-8")
    assert "def normalize" not in source
    caller = pathlib.Path("pipeline.py").read_text(encoding="utf-8")
    assert "normalize" not in caller
