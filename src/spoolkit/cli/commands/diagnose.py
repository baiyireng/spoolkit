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
from pathlib import Path

from spoolkit import diagnosis
from spoolkit.cli.term import escape, print_err, print_out


def diagnose_command(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()

    if args.init_key:
        key = diagnosis.ensure_key()
        print_out(
            f"[ok]签名密钥已生成：[/]{escape(str(diagnosis.key_path()))}"
            f"[muted]（{len(key) * 8} 位）[/]"
        )
        return 0

    if args.report:
        if not args.verdict:
            print_err(
                "--report 需要同时给出 --verdict。",
                markup=False,
                style="bad",
            )
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
            print_err(str(exc), markup=False, style="bad")
            return 2
        print_out(
            f"[ok]已写回报告 [/][accent]{escape(request.id)}[/]"
            "[muted]。Agent 可用 read_diagnosis 读取。[/]"
        )
        return 0

    requests = diagnosis.load_requests(root)
    if not requests:
        print_out("没有诊断请求。", markup=False, style="muted")
        return 0

    if args.show:
        target = diagnosis.find_request(root, args.show)
        if target is None:
            print_err(
                f"没有这条诊断请求：{args.show}",
                markup=False,
                style="bad",
            )
            return 2
        print_out(f"[muted]编号：[/][accent]{escape(target.id)}[/]")
        print_out(f"[muted]问题：[/]{escape(target.question)}")
        if target.hypothesis:
            print_out(f"[muted]假设：[/]{escape(target.hypothesis)}")
        if target.evidence:
            print_out(
                f"[muted]Agent 看到的证据：[/]{escape(target.evidence)}"
            )
        if target.report:
            report, verified = diagnosis.read_report(root, target.id)
            seal = "已验签" if verified else "未验签"
            seal_style = "ok" if verified else "slow"
            print_out(f"[muted]报告（[/][{seal_style}]{seal}[/][muted]）：[/]")
            for key, value in report.items():
                print_out(
                    f"  [accent]{escape(str(key))}[/][muted]:[/] "
                    f"{escape(str(value))}"
                )
        else:
            print_out("（尚未有报告）", markup=False, style="muted")
        return 0

    for item in requests:
        if item.answered:
            mark = "[ok]已回复[/]"
        else:
            mark = "[state.slow]待验证[/]"
        print_out(
            f"[accent]{escape(item.id)}[/]  {mark}  {escape(item.question)}"
        )
    print_out("")
    print_out(
        "[muted]用 --show <id> 查看详情，用 --report <id> --verdict ... 写回结论。[/]"
    )
    return 0
