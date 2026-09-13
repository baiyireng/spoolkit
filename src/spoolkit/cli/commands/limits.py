"""查看与覆盖能力标定值。

```
spool limits                       # 现在生效的是多少、从哪来
spool limits --set repeat_block_at=5
spool limits --reset repeat_block_at
```

为什么需要它：一条**看不见来源**的限制，用起来和写死的限制一样难受——
你不知道该不该动它、动到多少合适。而默认值是**按本机 27B 实测**的那一套，
不是普适真理；换到远程强模型上，该动的正是这些「防退化」的阈值。
"""

import argparse
import sys
from pathlib import Path

from spoolkit import limits


def limits_command(args: argparse.Namespace) -> int:
    project_root = Path(args.root).resolve()
    overrides = limits.load_overrides(project_root)

    if args.set:
        changed = 0
        for item in args.set:
            if "=" not in item:
                print(f"要写成 name=value：{item}", file=sys.stderr)
                return 2
            name, _, raw = item.partition("=")
            name = name.strip()
            entry = limits.knob(name)
            if entry is None:
                print(f"没有登记过这个标定值：{name}", file=sys.stderr)
                print("用 `spool limits` 看全部名字。", file=sys.stderr)
                return 2
            if not entry.overridable:
                print(
                    f"{name} 是安全边界（{entry.note}），不提供覆盖入口——"
                    "它防的是不可逆后果，和模型强弱无关。",
                    file=sys.stderr,
                )
                return 2
            try:
                overrides[name] = float(raw.strip())
            except ValueError:
                print(f"值要是个数：{item}", file=sys.stderr)
                return 2
            changed += 1
        path = limits.save_overrides(project_root, overrides)
        print(f"已写入 {path}（{changed} 项）")

    if args.reset:
        for name in args.reset:
            overrides.pop(name, None)
        limits.save_overrides(project_root, overrides)
        print("已恢复默认：" + "、".join(args.reset))

    print(limits.render(overrides))
    return 0
