"""变换层：把行洗干净、补上派生字段。"""

from pipeline.fetch import get_rows


def normalize(rows):
    """去掉名字为空的行，金额四舍五入到两位。"""
    cleaned = []
    for row in rows:
        if not row["name"]:
            continue
        cleaned.append(
            {
                "id": row["id"],
                "name": row["name"],
                "amount": round(row["amount"], 2),
            }
        )
    return cleaned


def load_and_normalize(source):
    """一次做完：读 + 洗。命令行和报表都走这一条。"""
    return normalize(get_rows(source))
