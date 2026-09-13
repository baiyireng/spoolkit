"""诊断通道的「特权侧」。

Agent 只能登记问题；回答它需要真实环境权限，所以这一步刻意做成一个
**由人（或人来跑的外部会话）执行**的命令。这不是仪式感：把回答权留在
受限 Agent 之外，是这套机制唯一的安全边界。

典型用法：

    spool diagnose --list                  # 看有哪些待验证的问题
    spool diagnose --show <id>             # 看完整的问题与证据
    spool diagnose --report <id> \\
        --verdict "临时目录不可写" \\
        --findings "pytest 的 basetemp 落在了一个权限损坏的目录上" \\
        --evidence "手动在别的 basetemp 下跑，同一套测试全绿"
"""

import argparse
import sys
from pathlib import Path

from spoolkit import diagnosis


def diagnose_command(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()

    if args.init_key:
        key = diagnosis.ensure_key()
        print(f"签名密钥已就绪：{diagnosis.key_path()}（{len(key) * 8} 位）")
        return 0

    if args.report:
        if not args.verdict:
            print("--report 需要同时给出 --verdict。", file=sys.stderr)
            return 2
        try:
            request = diagnosis.write_report(
                root,
                args.report,
                verdict=args.verdict,
                findings=args.findings,
                evidence=args.evidence,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"已写回报告 {request.id}。Agent 可以用 read_diagnosis 读到它。")
        return 0

    requests = diagnosis.load_requests(root)
    if not requests:
        print("没有诊断请求。")
        return 0

    if args.show:
        target = diagnosis.find_request(root, args.show)
        if target is None:
            print(f"没有这条诊断请求: {args.show}", file=sys.stderr)
            return 2
        print(f"编号：{target.id}")
        print(f"问题：{target.question}")
        if target.hypothesis:
            print(f"假设：{target.hypothesis}")
        if target.evidence:
            print(f"Agent 看到的证据：{target.evidence}")
        if target.report:
            report, verified = diagnosis.read_report(root, target.id)
            print(f"报告（{'已验签' if verified else '未验签'}）：")
            for key, value in report.items():
                print(f"  {key}: {value}")
        else:
            print("（还没有报告）")
        return 0

    for item in requests:
        mark = "已回复" if item.answered else "待验证"
        print(f"{item.id}  [{mark}]  {item.question}")
    print()
    print("用 --show <id> 看详情，--report <id> --verdict ... 写回结论。")
    return 0
