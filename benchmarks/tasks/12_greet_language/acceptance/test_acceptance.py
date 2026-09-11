from greet import greet


def test_default_stays_english():
    assert greet("Ann") == "Hello, Ann!"


def test_chinese():
    assert greet("Ann", "zh") == "你好，Ann！"
