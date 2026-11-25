"""按线上同一形状造一份日志，用来在本地量耗时。

    python gen_log.py 40000 > data/big.log

路径分布刻意做得和线上接近：一成半的请求落在少数几个热路径上，剩下的是
**很长的长尾**——商品详情页那种 id 各不相同的路径。生成器带固定种子，
所以同一行数每次得到的文件完全一样。
"""

import random
import sys

HOT = ["/api/items", "/api/items/", "/api/search", "/static/app.js", "/", "/login"]
HOT_SHARE = 0.15
TAIL_ID_RANGE = 2_000_000
AGENTS = ["127.0.0.1", "10.0.3.11", "10.0.3.12", "172.16.5.9"]
CODES = [(200, 82), (304, 8), (404, 5), (500, 2), (301, 3)]


def pick_code(rng):
    roll = rng.randint(1, 100)
    total = 0
    for code, weight in CODES:
        total += weight
        if roll <= total:
            return code
    return 200


def main(count):
    rng = random.Random(20250312)
    out = []
    for index in range(count):
        if rng.random() < HOT_SHARE:
            path = rng.choice(HOT)
        else:
            path = f"/api/items/{rng.randint(1, TAIL_ID_RANGE)}"
        minute = index % 60
        second = index % 60
        out.append(
            f'{rng.choice(AGENTS)} - - [12/Mar/2025:10:{minute:02d}:{second:02d} +0800] '
            f'"GET {path} HTTP/1.1" {pick_code(rng)} {rng.randint(120, 9000)}'
        )
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    total = int(sys.argv[1]) if len(sys.argv) > 1 else 4000
    sys.stdout.write(main(total))
