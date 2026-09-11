def find_by_name(cursor, name):
    cursor.execute("SELECT id FROM users WHERE name = '" + name + "'")
    return cursor.fetchall()
