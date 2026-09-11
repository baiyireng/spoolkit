import inspect

from app import run
from greeter import welcome


def test_run_still_works():
    assert run() == "Welcome, Ann!"


def test_signature_has_one_parameter():
    assert list(inspect.signature(welcome).parameters) == ["name"]
