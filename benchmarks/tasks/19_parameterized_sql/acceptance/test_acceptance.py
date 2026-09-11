import sqlite3

from store import find_by_name


def _cursor():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE users (id INTEGER, name TEXT)")
    conn.execute("INSERT INTO users VALUES (1, 'O''Brien')")
    conn.execute("INSERT INTO users VALUES (2, 'Ann')")
    return conn.cursor()


def test_name_with_quote():
    assert find_by_name(_cursor(), "O'Brien") == [(1,)]


def test_plain_name():
    assert find_by_name(_cursor(), "Ann") == [(2,)]
