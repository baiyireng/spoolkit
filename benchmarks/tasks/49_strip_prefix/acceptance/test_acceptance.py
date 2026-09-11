from prefix import without_prefix


def test_only_leading_prefix():
    # 用 "ab" 而不是 "ab-"：后者用 replace 恰好也能得到正确结果，
    # 那样的用例区分不了修没修（这条是被完整性测试抓出来的）。
    assert without_prefix("ab-ab", "ab") == "-ab"


def test_no_prefix():
    assert without_prefix("xy", "ab") == "xy"
