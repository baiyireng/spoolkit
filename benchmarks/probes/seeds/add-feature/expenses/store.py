"""账本数据的读取与汇总。

账本是一份 CSV：`date,category,amount,note`，`date` 是 `YYYY-MM-DD`。
金额一律用 float 再格式化到两位小数——这是个人记账，不涉及分毫的舍入争议。
"""

import csv
from pathlib import Path

FIELDS = ("date", "category", "amount", "note")


def load(path):
    """读整本账，返回 dict 列表（按文件里的顺序）。"""
    rows = []
    with Path(path).open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for record in reader:
            rows.append(
                {
                    "date": record["date"].strip(),
                    "category": record["category"].strip(),
                    "amount": float(record["amount"]),
                    "note": (record.get("note") or "").strip(),
                }
            )
    return rows


def total(rows):
    """总额。"""
    return sum(row["amount"] for row in rows)


def by_category(rows):
    """分类小计：分类名 -> 金额。"""
    summary = {}
    for row in rows:
        name = row["category"]
        summary[name] = summary.get(name, 0.0) + row["amount"]
    return summary
