"""命令行入口：python stat.py <日志文件>"""

import sys

from logstat.report import render, summarize


def main(argv):
    if len(argv) < 2:
        print("用法：python stat.py <日志文件>", file=sys.stderr)
        return 2
    print(render(summarize(argv[1])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
