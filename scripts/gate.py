"""本仓唯一的开发关口：环境体检 + 测试 + 类型检查。

    python scripts/gate.py                    # 全套（体检 + 测试 + 类型）
    python scripts/gate.py --only doctor      # 只做环境体检
    python scripts/gate.py --only tests       # 只跑测试
    python scripts/gate.py --only types       # 只跑类型检查
    python scripts/gate.py --update-baseline  # 类型检查：刷新基线

**环境准备只有一处**：全部来自 `scripts/dev_env.py`（能力体检 + 环境变量）。
以前这段在 `pytest.ps1` 与这里各写一遍，是"同一类坑每次重查"的源头之一。

**红灯要分得清"环境"与"回归"**（这是这个关口最重要的性质）：

- 用例自己声明依赖：`@pytest.mark.needs("process_kill")`（`scripts/pytest_needs.py`）
  → 缺能力就**跳过并说明缺什么**；
- 兜底清单 `tools/known-env-failures.json`：给"不方便逐个打标记"的文件用，
  每条必须带 `requires`（缺哪个能力）、`evidence`（证据）、`expires`（到期日）。
  **能力回来即失效**：能力可用时那条用例红了就是真回归，不再豁免；
  **非受限环境里豁免整体失效**；**过期条目直接让关口红**——豁免不许变成免罪牌。

**类型检查为什么带基线**：本仓有 160 条左右的存量发现（`tools/mypy-baseline.json`）。
一步到位压死开发的结果是没人上；所以走**棘轮**：存量记账、**只对新增报红**，
修掉的从基线里删。基线里记着 mypy 与 python 版本——检查器换版本会让结论整体
漂移，那必须是一次**显式**批次（`--update-baseline`）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import dev_env

REPO = dev_env.REPO
BASELINE = REPO / "tools" / "mypy-baseline.json"
ENV_FAILURES = REPO / "tools" / "known-env-failures.json"
MYPY_TARGET = "src/spoolkit"
BASELINE_VERSION = 1

_ERROR_LINE = re.compile(
    r"^(?P<path>[^:]+):(?P<line>\d+)(?::(?P<col>\d+))?: (?P<severity>error|warning|note): "
    r"(?P<message>.*?)(?:\s+\[(?P<code>[a-z-]+)\])?$"
)
_FAILED_LINE = re.compile(r"^FAILED (?P<id>[^ ]+)", re.M)


# ---------------------------------------------------------------- 测试


def _parse_failed(output: str) -> list[str]:
    return [match.group("id") for match in _FAILED_LINE.finditer(output)]


def _capability(name: str, capabilities: dict) -> tuple[bool, str]:
    item = capabilities.get(name)
    if item is None:
        return False, f"未知能力：{name}（去 dev_env.py 登记）"
    return bool(item["ok"]), str(item["detail"])


def load_env_failures(capabilities: dict) -> tuple[list[dict], list[str]]:
    """读兜底清单，返回（本次生效的条目，必须报出来的问题）。"""
    if not ENV_FAILURES.is_file():
        return [], []
    payload = json.loads(ENV_FAILURES.read_text(encoding="utf-8"))
    entries = list(payload.get("entries") or [])
    sandboxed = dev_env.is_sandboxed(capabilities)
    today = dt.date.today()
    active: list[dict] = []
    problems: list[str] = []
    for entry in entries:
        where = entry.get("id") or entry.get("file") or "（没写 id/file）"
        expiry = entry.get("expires") or ""
        try:
            expired = dt.date.fromisoformat(str(expiry)) < today
        except ValueError:
            problems.append(f"{where}：expires 不是 YYYY-MM-DD（现在是 {expiry!r}）")
            continue
        if expired:
            problems.append(f"{where}：豁免已过期（{expiry}）——复查后删掉或续期")
            continue
        if not sandboxed:
            problems.append(
                f"{where}：当前**不是受限环境**，豁免不生效（正常环境里它必须绿）"
            )
            continue
        ok, detail = _capability(str(entry.get("requires") or ""), capabilities)
        if ok:
            problems.append(
                f"{where}：能力 {entry.get('requires')} 现在可用，豁免失效"
                "（它要是红了就是真回归）"
            )
            continue
        active.append({**entry, "_detail": detail})
    return active, problems


def _exempt_for(test_id: str, active: list[dict]) -> dict | None:
    for entry in active:
        if entry.get("id") and entry["id"] == test_id:
            return entry
        if entry.get("file") and test_id.startswith(str(entry["file"])):
            return entry
    return None


def run_tests(extra: list[str], capabilities: dict) -> int:
    command = [
        sys.executable, "-m", "pytest", "-q",
        # `-rs`：跳过原因要印出来。缺能力而跳过不是"没跑"——读的人必须能看见
        # "缺什么、为什么"，否则绿灯里那几条跳过就变成了没人知道的窟窿。
        "-rs",
        "-p", "no:cacheprovider",
        "-p", "pytest_sandbox_compat",
        "-p", "pytest_hang_guard",
        "-p", "pytest_needs",
        *extra,
    ]
    print("── 测试 ──")
    print(" ".join(command))
    done = subprocess.run(
        command, cwd=str(REPO), env=dev_env.test_env(),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    output = (done.stdout or "") + (done.stderr or "")
    print(output.rstrip())

    failed = _parse_failed(output)
    active, problems = load_env_failures(capabilities)
    exempted: list[tuple[str, dict]] = []
    real: list[str] = []
    for test_id in failed:
        entry = _exempt_for(test_id, active)
        if entry is None:
            real.append(test_id)
        else:
            exempted.append((test_id, entry))

    print()
    if exempted:
        print("环境豁免（缺能力导致，**不算失败**）：")
        for test_id, entry in exempted:
            print(
                f"  - {test_id}  需 {entry.get('requires')}（{entry.get('_detail')}）"
                f"；到期 {entry.get('expires')}"
            )
            info = dev_env.CAPABILITIES.get(str(entry.get("requires")))
            if info:
                # **所以呢**：豁免只回答"为什么红"，下一步回答"那我怎么办"。
                print(f"      受限下这么做：{info.workaround}")
                if info.escalate:
                    print(f"      {info.escalate}")
    if real:
        print("真失败：")
        for test_id in real:
            print(f"  - {test_id}")
    for problem in problems:
        print(f"⚠ 豁免清单问题：{problem}")
    if failed and not real and not problems:
        print("（本次没有真失败：红的全是缺能力造成的）")
    if not failed and not problems:
        print("没有失败。")
    # 采集错误（导入期异常之类）不会有 FAILED 行，但退出码非 0：不能当成"没问题"。
    collection_error = done.returncode not in (0, 1) and not failed
    if collection_error:
        print(f"⚠ pytest 退出码 {done.returncode}，但没有 FAILED 行（收集期出错？）")
    return 1 if (real or problems or collection_error) else 0


# ---------------------------------------------------------------- 类型


def _mypy_version() -> str:
    done = subprocess.run(
        [sys.executable, "-m", "mypy", "--version"], cwd=str(REPO),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    text = (done.stdout or done.stderr or "").strip()
    match = re.search(r"\d+\.\d+\.\d+", text)
    return match.group(0) if match else (text.splitlines()[0] if text else "?")


def _mypy_findings() -> dict[str, int]:
    done = subprocess.run(
        [sys.executable, "-m", "mypy", "--no-error-summary", "--no-pretty", MYPY_TARGET],
        cwd=str(REPO), env=dict(os.environ),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    findings: dict[str, int] = {}
    for raw in (done.stdout or "").splitlines():
        match = _ERROR_LINE.match(raw.strip())
        if not match or match.group("severity") != "error":
            continue
        path = match.group("path").replace("\\", "/").lstrip("./")
        key = f"{path}|{match.group('code') or '?'}|{match.group('message').strip()}"
        findings[key] = findings.get(key, 0) + 1
    return findings


def run_types(update_baseline: bool) -> int:
    print("── 类型检查（mypy，带基线棘轮）──")
    findings = _mypy_findings()
    version = _mypy_version()
    if update_baseline:
        payload = {
            "version": BASELINE_VERSION,
            "mypy": version,
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "note": (
                "存量发现的账。**只对新发现报红**（scripts/gate.py）："
                "刷新这份文件等于承认这一批债务，必须是一次显式提交。"
            ),
            "entries": dict(sorted(findings.items())),
        }
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"基线已刷新：{BASELINE.relative_to(REPO)}（{len(findings)} 类 / "
              f"{sum(findings.values())} 条；mypy {version}）")
        return 0

    if not BASELINE.is_file():
        print(f"没有基线文件（{BASELINE.relative_to(REPO)}）：先跑一次 --update-baseline。")
        return 1
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    old = dict(baseline.get("entries") or {})
    if baseline.get("mypy") and baseline["mypy"] != version:
        print(
            f"⚠ 基线是 {baseline['mypy']} 时记的，现在是 {version}——"
            "检查器换版本会让结论整体漂移，请把刷新基线当成一次独立批次。"
        )
    new_keys = sorted(set(findings) - set(old))
    grown = sorted(key for key in set(findings) & set(old) if findings[key] > old[key])
    shrunk = sorted(key for key in set(old) - set(findings))
    print(f"存量（基线）{sum(old.values())} 条；本次 {sum(findings.values())} 条")
    if new_keys or grown:
        print()
        print("发现**新增**（要么修掉，要么在提交里显式刷新基线）：")
        for key in new_keys:
            path, code, message = key.split("|", 2)
            print(f"  + [{code}] {path}：{message}")
        for key in grown:
            path, code, message = key.split("|", 2)
            print(f"  + [{code}] {path}：{message}（{old[key]} → {findings[key]} 条）")
        return 1
    if shrunk:
        print(f"（有 {len(shrunk)} 类已修掉，可以从基线里删了：跑 --update-baseline 收账）")
    print("没有新增发现。")
    return 0


# ---------------------------------------------------------------- 入口


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本仓唯一的开发关口：体检 + 测试 + 类型检查")
    parser.add_argument("--only", choices=["doctor", "tests", "types"], default="")
    parser.add_argument("--update-baseline", action="store_true", help="刷新类型检查基线")
    parser.add_argument("pytest_args", nargs="*", help="透传给 pytest 的参数（只在跑测试时用）")
    args = parser.parse_args(argv)

    capabilities = dev_env.probe_capabilities()
    codes: list[int] = []
    if args.only in ("", "doctor"):
        print("── 环境体检 ──")
        print(dev_env.render(capabilities))
        print()
    if args.only in ("", "tests"):
        codes.append(run_tests(args.pytest_args, capabilities))
    if args.only in ("", "types"):
        codes.append(run_types(args.update_baseline))
    result = 0 if all(code == 0 for code in codes) else 1
    print()
    print("关口：" + ("通过" if result == 0 else "**未通过**"))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
