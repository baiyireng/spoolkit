"""报表层：把洗好的行汇总成文本。"""

import pipeline.fetch as fetch


def render(source):
    """读源并渲染一份纯文本报表。"""
    rows = fetch.get_rows(source)
    total = 0.0
    lines = []
    for row in rows:
        if not row["name"]:
            continue
        total += row["amount"]
        lines.append(f"{row['id']} {row['name']} {row['amount']:.2f}")
    lines.append(f"合计 {total:.2f}")
    return "\n".join(lines)
