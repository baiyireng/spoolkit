from lines import line_count


def test_trailing_newline_not_counted():
    assert line_count("a\nb\n") == 2


def test_no_trailing_newline():
    assert line_count("a\nb") == 2
