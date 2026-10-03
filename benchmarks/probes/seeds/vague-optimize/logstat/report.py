"""日志统计：按状态码与路径汇总。

输入是 nginx 默认格式的 access log 一行：

    127.0.0.1 - - [12/Mar/2025:10:00:01 +0800] "GET /api/items HTTP/1.1" 200 512

汇总结果是要贴进日报的，所以**结果必须逐字稳定**：
`top_paths` 按次数降序、次数相同按路径字典序升序，取前 5 条。
"""

from pathlib import Path

TOP_LIMIT = 5


def parse_line(line):
    """解析一行日志；解析不出来返回 None。"""
    parts = line.split('"')
    if len(parts) < 3:
        return None
    request = parts[1].split()
    if len(request) < 2:
        return None
    tail = parts[2].split()
    if len(tail) < 2:
        return None
    try:
        code = int(tail[0])
    except ValueError:
        return None
    return {"method": request[0], "path": request[1], "code": code}


def collect(log_path):
    """读整个文件，返回解析成功的行。"""
    rows = []
    for line in Path(log_path).read_text(encoding="utf-8").splitlines():
        row = parse_line(line)
        if row is not None:
            rows.append(row)
    return rows


def code_counts(rows):
    """状态码 -> 次数。"""
    counts = {}
    for row in rows:
        counts[row["code"]] = counts.get(row["code"], 0) + 1
    return counts


def path_counts(rows):
    """路径 -> 次数，返回 [[路径, 次数], ...]，按首次出现顺序。

    这是整份脚本最热的一段：每来一行都要在已有结果里找一遍同一个路径。
    目前够用，但日志涨上来之后它就成了瓶颈。
    """
    counts = []
    for row in rows:
        hit = None
        for item in counts:
            if item[0] == row["path"]:
                hit = item
                break
        if hit is None:
            counts.append([row["path"], 1])
        else:
            hit[1] += 1
    return counts


def top_paths(rows, limit=TOP_LIMIT):
    """访问最多的前 limit 条路径。次序是结果的一部分，别乱动。"""
    counts = path_counts(rows)
    counts.sort(key=lambda item: (-item[1], item[0]))
    return counts[:limit]


def summarize(log_path):
    """一份可以直接打印的摘要。"""
    rows = collect(log_path)
    return {
        "total": len(rows),
        "codes": code_counts(rows),
        "top_paths": top_paths(rows),
    }


def render(summary):
    """把摘要渲染成给日报用的多行文本。"""
    lines = [f"总请求数：{summary['total']}"]
    for code in sorted(summary["codes"]):
        lines.append(f"状态码 {code}：{summary['codes'][code]}")
    lines.append("热门路径：")
    for path, hits in summary["top_paths"]:
        lines.append(f"  {path} {hits}")
    return "\n".join(lines)
