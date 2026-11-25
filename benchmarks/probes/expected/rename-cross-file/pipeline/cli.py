"""命令行：python -m pipeline.cli <源文件>"""

import sys

from pipeline.report import render


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("用法：python -m pipeline.cli <源文件>", file=sys.stderr)
        return 2
    print(render(argv[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
