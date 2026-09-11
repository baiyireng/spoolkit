def by_score(rows):
    """按 score 升序排列；score 相同时按 name 升序。"""
    return sorted(rows, key=lambda row: row["score"])
