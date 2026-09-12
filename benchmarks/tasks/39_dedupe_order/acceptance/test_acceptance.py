from dedupe import unique


def test_keeps_first_occurrence_order():
    # 用整数而不是字符串：字符串的哈希是随机的，set 的迭代顺序会随进程变化，
    # 有时候恰好等于期望顺序——那会让这道题随机地「未修改就通过」。
    # 小整数的迭代顺序是稳定的（升序），稳定地不等于期望顺序。
    assert unique([3, 1, 2, 1]) == [3, 1, 2]


def test_drops_duplicates():
    assert sorted(unique([5, 5, 5])) == [5]
