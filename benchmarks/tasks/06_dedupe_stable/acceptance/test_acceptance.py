from lists import dedupe


def test_stable_order():
    # 用整数而不是字符串：字符串哈希每个进程都不同，set 的迭代顺序随之变化，
    # 会让这个任务时对时错——flaky 的任务比坏任务更糟。
    assert dedupe([5, 3, 5, 1, 3]) == [5, 3, 1]


def test_empty():
    assert dedupe([]) == []


def test_keeps_first_seen_order():
    assert dedupe([2, 1, 2, 3, 1, 4]) == [2, 1, 3, 4]
