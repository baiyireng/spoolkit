"""抓取层：把上游给的东西变成统一的行。"""

import csv
from pathlib import Path


def load_rows(source):
    """读一个 CSV 源，返回 dict 列表。

    上游有几个不同的导出格式，但列名已经统一过了：`id,name,amount`。
    """
    rows = []
    with Path(source).open(encoding="utf-8", newline="") as handle:
        for record in csv.DictReader(handle):
            rows.append(
                {
                    "id": record["id"].strip(),
                    "name": record["name"].strip(),
                    "amount": float(record["amount"]),
                }
            )
    return rows
