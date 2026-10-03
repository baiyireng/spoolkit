"""真实任务能力探针的跑分器。

为什么要有它：连续四次在**真实任务**上发现同一类缺陷（HTML 任务却只给 JS 验证器、
砍半护栏挡住合法的整文件大重写、重写确认要重发整份正文把输出预算挤爆、拦读与
上下文压缩耦合导致任务死锁），而**合成的 50 题 bench 一个都测不到**——那些题
全是一次小改。所以这里放的是「用户会真的说的话 + 真的产物」：每个探针有自己的
种子工作区、自己的沙箱副本、自己的验收断言，跑完落一条结构化记录，下一轮修完
拿同一批探针复跑，数字可以直接对着看。

用法：

    python scripts/probe.py --list
    python scripts/probe.py --probe merge-html --provider llamacpp --base-url http://127.0.0.1:8080
    python scripts/probe.py --all --root D:\\tmp\\probe-runs --total-budget 5400

三条不变式，写在最前面是因为它们是这个脚本存在的理由：

1. **绝不动种子目录、绝不动用户工作区。** 每个探针在 `--root` 下自己的沙箱副本里
   跑（`<root>/<id>`），跑完还要再算一遍种子指纹确认没被污染。
2. **工作区级授权必须真的生效。** 跑之前往沙箱写 `.agent/policy.json`
   （`{"policy":"auto"}`），命令行**不传** `--policy`——这样授权的生效路径就是
   真实的「读工作区策略」那条。写文件必须**无 BOM**：PowerShell 5.1 的
   `-Encoding utf8` 会带 BOM，而 `load_policy` 读不了 BOM 会静默退回 `ask`，
   于是每一步写操作都要确认，无人值守的探针直接跑挂。
3. **一次跑分有界。** 单探针超时、步数上限、整批总预算都可配；超时就强杀进程树
   并如实记录，绝不挂死。
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROBES_DIR = REPO_ROOT / "benchmarks" / "probes"
MANIFEST_PATH = PROBES_DIR / "probes.json"
RECORDS_DIR = PROBES_DIR / "records"
SUMMARY_PATH = PROBES_DIR / "summary.json"
RESULTS_JSONL = PROBES_DIR / "results.jsonl"

# 验收要求的五种任务形状。清单里少一种就说明探针集退化了。
REQUIRED_SHAPES = (
    "merge-html",
    "vague-optimize",
    "add-feature",
    "fix-bug",
    "rename-cross-file",
)

REQUIRED_PROBE_KEYS = ("id", "shape", "title", "seed", "expected", "goal", "check")

# 记录里必须有的字段。少了任何一条，这份记录就没法拿来跟以后对比。
REQUIRED_RECORD_KEYS = (
    "id",
    "goal",
    "started_at",
    "wall_seconds",
    "exit_code",
    "final_ok",
    "steps",
    "model_calls",
    "prompt_tokens",
    "completion_tokens",
    "verifier_ran",
    "verifier_ok",
    "repeat_blocks",
    "read_blocks",
    "supervisor_interventions",
    "supervisor_false_claims",
    "file_changed",
    "check_ok",
    "notes",
)

# ---- 事件流里的字面量 -------------------------------------------------------
# 判定全靠这些字面量，所以集中放一处：改话术时这里跟着改，别散落在解析逻辑里。
BLOCKED_PREFIX = "⛔ 未执行："
REFUSAL_MARKER = "⚠ 拒绝整文件重写"
IDENTICAL_RETRY_MARK = "完全相同"
REPEAT_GUARD_MARK = "已经做过"
VERIFIER_NAMES = ("自动验证", "最终验证")
READ_TOOLS = frozenset(
    {"read_file", "list_dir", "search_code", "dir_stats", "read_diagnosis"}
)
SUPERVISOR_PREFIX = "督导："
GUARD_REFRESH_KIND = "guard_refresh"
FACT_CONFLICT_KIND = "supervisor_fact_conflict"

POLICY_RELATIVE = Path(".agent") / "policy.json"


class ManifestError(RuntimeError):
    """清单不合法。宁可开跑前就报错，也不要跑到一半才发现。"""


# --------------------------------------------------------------------------
# 清单
# --------------------------------------------------------------------------


def load_manifest(path=None):
    """读并校验探针清单。任何结构问题都抛 ManifestError。"""
    path = Path(path or MANIFEST_PATH)
    if not path.is_file():
        raise ManifestError(f"找不到探针清单：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"探针清单不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise ManifestError("探针清单的根必须是一个对象")
    defaults = payload.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ManifestError("defaults 必须是一个对象")
    probes = payload.get("probes")
    if not isinstance(probes, list) or not probes:
        raise ManifestError("probes 必须是一个非空数组")

    seen = set()
    for index, probe in enumerate(probes):
        where = f"probes[{index}]"
        if not isinstance(probe, dict):
            raise ManifestError(f"{where} 必须是一个对象")
        for key in REQUIRED_PROBE_KEYS:
            if key not in probe:
                raise ManifestError(f"{where} 缺字段 {key}")
        if not isinstance(probe["id"], str) or not probe["id"].strip():
            raise ManifestError(f"{where}.id 必须是非空字符串")
        identifier = probe["id"].strip()
        if identifier in seen:
            raise ManifestError(f"探针 id 重复：{identifier}")
        seen.add(identifier)
        if not isinstance(probe["goal"], str) or not probe["goal"].strip():
            raise ManifestError(f"{where}.goal 必须是非空字符串")
        check = probe["check"]
        if not isinstance(check, dict) or not check.get("script"):
            raise ManifestError(f"{where}.check.script 必须是相对路径")
        for key in ("seed", "expected"):
            target = (PROBES_DIR / probe[key]).resolve()
            if not target.is_dir():
                raise ManifestError(f"{where}.{key} 不存在：{target}")
        script = (PROBES_DIR / check["script"]).resolve()
        if not script.is_file():
            raise ManifestError(f"{where}.check.script 不存在：{script}")
        guards = probe.get("guard_hashes") or {}
        if not isinstance(guards, dict):
            raise ManifestError(f"{where}.guard_hashes 必须是对象")
    return {"version": payload.get("version", 1), "defaults": defaults, "probes": probes}


def find_probe(manifest, identifier):
    for probe in manifest["probes"]:
        if probe["id"] == identifier:
            return probe
    raise ManifestError(f"清单里没有这个探针：{identifier}")


# --------------------------------------------------------------------------
# 沙箱与授权
# --------------------------------------------------------------------------


def default_root():
    """不给 --root 时的沙箱根：系统临时目录，一次跑分一个时间戳。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return Path(tempfile.gettempdir()) / "spoolkit-probes" / stamp


def materialize(seed, sandbox, root):
    """把种子复制成沙箱副本，返回沙箱路径。

    三道闸都是防「手滑把种子或用户工作区删了」：目标必须是 `--root` 的直接子目录，
    而且不能落在种子/期望版本里面。删只删这一个目录。
    """
    seed = Path(seed).resolve()
    sandbox = Path(sandbox).resolve()
    root = Path(root).resolve()

    if sandbox.parent != root:
        raise ValueError(f"沙箱必须是 --root 的直接子目录：{sandbox}（root={root}）")
    if sandbox == seed or seed in sandbox.parents or sandbox in seed.parents:
        raise ValueError(f"沙箱不能与种子目录重叠：沙箱 {sandbox}，种子 {seed}")
    if sandbox == REPO_ROOT or REPO_ROOT in sandbox.parents:
        raise ValueError(f"沙箱不能落在仓库里：{sandbox}")

    if sandbox.exists():
        shutil.rmtree(sandbox)
    shutil.copytree(seed, sandbox)
    return sandbox


def write_policy(sandbox, policy="auto"):
    """写工作区级授权。**必须无 BOM**，见模块开头的第 2 条不变式。"""
    path = Path(sandbox) / POLICY_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"policy": policy}, ensure_ascii=False, indent=2)
    # 显式 utf-8（不是 utf-8-sig），写完再读回来确认头三个字节不是 BOM：
    # 这条不变式坏掉时的症状是「探针跑挂」，而原因离现场很远，所以就地钉住。
    path.write_text(payload, encoding="utf-8", newline="\n")
    data = path.read_bytes()
    if data[:3] == b"\xef\xbb\xbf":
        raise ValueError(f"授权文件被写成了带 BOM 的 UTF-8：{path}")
    return path


# --------------------------------------------------------------------------
# 指纹
# --------------------------------------------------------------------------


# 指纹要跳过的目录：字节码与测试缓存是「这里跑过一次」的痕迹，不是产物。
#
# 为什么必须跳：`check` 会 import 沙箱里的模块、跑沙箱里的测试，于是每次都会
# 多出一串 `.pyc`。不跳的话 `file_changed` 里全是这些噪声，而「模型到底改了
# 哪个文件」这个信号会被盖掉（第一版就是这样：added 里塞满 .agent 与 .pyc）。
# 另外**种子与期望版本里根本不该有它们**，那说明有人直接在种子目录里跑过 python，
# 那条由 tests/test_probe_runner.py 钉着。
CACHE_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})


def fingerprint_tree(root):
    """路径 -> 内容 sha256。用来证明「种子没被污染」和「结果真的变了」。"""
    root = Path(root)
    files = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in CACHE_DIRS for part in relative.parts):
            continue
        if path.suffix == ".pyc":
            continue
        files[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def digest(files):
    """把一份指纹折成一个数，方便一眼对比。"""
    material = "".join(f"{name}:{value}\n" for name, value in sorted(files.items()))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def diff_trees(before, after):
    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "modified": sorted(
            name for name in set(before) & set(after) if before[name] != after[name]
        ),
    }


def verify_guards(sandbox, guards):
    """清单里钉住的文件是否原样。改测试/改数据来凑绿会被这条拦下。"""
    failed = []
    for rel, want in sorted((guards or {}).items()):
        target = Path(sandbox) / rel
        if not target.is_file():
            failed.append(f"{rel} 不见了")
            continue
        got = hashlib.sha256(target.read_bytes()).hexdigest()
        if got != want:
            failed.append(f"{rel} 被改过（{got[:12]}… != {want[:12]}…）")
    return failed


# --------------------------------------------------------------------------
# 子进程
# --------------------------------------------------------------------------


def repo_python():
    """跑探针用的解释器：优先用仓库自己的 venv（spoolkit 装在那里）。"""
    candidates = [
        REPO_ROOT / ".venv" / "Scripts" / "python.exe",
        REPO_ROOT / ".venv" / "bin" / "python",
    ]
    for item in candidates:
        if item.is_file():
            return item
    return Path(sys.executable)


def repo_spool():
    """命令行入口：优先真的 console script，退而求其次走 `-m spoolkit.cli.app`。"""
    candidates = [
        REPO_ROOT / ".venv" / "Scripts" / "spool.exe",
        REPO_ROOT / ".venv" / "Scripts" / "spool",
        REPO_ROOT / ".venv" / "bin" / "spool",
    ]
    for item in candidates:
        if item.is_file():
            return [str(item)]
    return None


def _tool_dirs():
    """`rg` 与 `node` 的候选目录。

    `tools/search.py` 依赖 rg；HTML 的内联 JS 验证依赖 node。PATH 里没有它们时，
    「找不到 rg」会被记成工具失败、「找不到 node」会被 HTML 验证器报成
    「查不了」——两条都不是模型的问题，却会把探针读数染脏。所以这里补一次 PATH。
    """
    dirs = []
    local = os.environ.get("LOCALAPPDATA") or ""
    if local:
        versions = Path(local) / "cursor-agent" / "versions"
        if versions.is_dir():
            for item in sorted(versions.iterdir(), reverse=True):
                if item.is_dir():
                    dirs.append(item)
        dirs += [
            Path(local) / "Programs" / "cursor" / "resources" / "app" / "node_modules" / "@vscode" / "ripgrep" / "bin",
            Path(local) / "Microsoft" / "WinGet" / "Links",
            Path(local) / "Programs" / "Microsoft VS Code" / "bin",
        ]
    dirs += [Path(r"C:\Program Files\ripgrep"), Path("/usr/local/bin"), Path("/usr/bin")]
    return [item for item in dirs if item.is_dir()]


def probe_env(sandbox):
    """探针子进程的环境。

    三件事必须做对，否则读数会被环境污染：

    - `SPOOLKIT_STATE` 指进沙箱：不然探针工作区会被登记进**这台机器的**全局
      登记表，跑几十次之后别人的 `spool` 就开始看到一堆临时工作区。
    - `SPOOLKIT_CONFIG` / `SPOOLKIT_ENV_FILE` 指进沙箱：探针绝不读开发机的
      用户配置与 `.env`，否则「在我这儿能过」。
    - `PYTHONPATH` 指向本仓库 `src`：跑的是这份 checkout，不是碰巧装的那个。
    """
    sandbox = Path(sandbox)
    state = sandbox / ".agent" / "probe-state"
    state.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["SPOOLKIT_STATE"] = str(state)
    env["SPOOLKIT_CONFIG"] = str(sandbox / ".agent" / "probe-config.toml")
    env["SPOOLKIT_ENV_FILE"] = str(sandbox / ".agent" / "probe.env")
    for stale in ("AGENTS_DEV_CONFIG", "AGENTS_DEV_ENV_FILE", "AGENTS_DEV_STATE"):
        env.pop(stale, None)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "src"), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    added = []
    for tool in ("rg", "node"):
        if shutil.which(tool, path=env.get("PATH", "")) is not None:
            continue
        for directory in _tool_dirs():
            if (directory / f"{tool}.exe").is_file() or (directory / tool).is_file():
                env["PATH"] = f"{directory}{os.pathsep}{env.get('PATH', '')}"
                added.append(f"{tool}@{directory}")
                break
    env["_PROBE_PATH_ADDED"] = ";".join(added)
    return env


def build_command(probe, sandbox, options):
    """拼出这次要跑的 spool 命令。

    **不传 `--policy`**：授权的生效路径要走工作区里的 `.agent/policy.json`，
    那正是这条不变式要验的东西。`--scope` 是另一件事（自动落盘的范围），
    它没有工作区级存储，只能由命令行给。
    """
    entry = options.get("spool") or repo_spool()
    head = [str(item) for item in entry] if entry else [
        str(repo_python()),
        "-m",
        "spoolkit.cli.app",
    ]
    command = [
        *head,
        "run",
        "--events",
        "--root",
        str(sandbox),
        "--goal",
        probe["goal"],
        "--session",
        options["session"],
        "--provider",
        options["provider"],
        "--scope",
        options["scope"],
        "--max-steps",
        str(options["max_steps"]),
        "--step-ceiling",
        str(options["step_ceiling"]),
        "--subagent-steps",
        str(options["subagent_steps"]),
    ]
    if options.get("base_url"):
        command += ["--base-url", options["base_url"]]
    if options.get("model"):
        command += ["--model", options["model"]]
    if options.get("window"):
        command += ["--window", str(options["window"])]
    if options.get("no_supervise"):
        command.append("--no-supervise")
    command += list(options.get("extra_args") or [])
    return command


def kill_tree(pid):
    """连子进程一起杀掉。探针里会派生 node/python/pytest，只杀父进程会留一堆孤儿。"""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    try:
        os.killpg(os.getpgid(pid), 9)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def run_spool(command, *, cwd, timeout, stdout_path, stderr_path, env):
    """跑一次 spool，输出直接落文件（不走管道，免得大输出把管道堵死）。

    返回一条结果字典；超时不算异常，如实记下来。
    """
    started = time.time()
    creation = 0
    if os.name == "nt":
        creation = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        try:
            proc = subprocess.Popen(
                command,
                cwd=str(cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                creationflags=creation,
            )
        except OSError as exc:
            return {
                "exit_code": None,
                "timed_out": False,
                "spawn_error": f"{exc!r}",
                "wall_seconds": round(time.time() - started, 3),
            }
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_tree(proc.pid)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover - 极少见
                proc.kill()
                proc.wait()
    return {
        "exit_code": proc.returncode,
        "timed_out": timed_out,
        "spawn_error": "",
        "wall_seconds": round(time.time() - started, 3),
    }


# --------------------------------------------------------------------------
# 事件流
# --------------------------------------------------------------------------


def _blocked(detail):
    return detail.startswith(BLOCKED_PREFIX) or REFUSAL_MARKER in detail


def parse_events(text):
    """把 `--events` 的 JSON 行折成一组计数。

    每个计数都对应一条**能证伪**的事实，不是「大概看了看日志」：
    `repeat_blocks` / `read_blocks` / `supervisor_false_claims` 直接从事件里的
    工具名与 note 的 kind 读出来，不看模型的总结文字。
    """
    counters = {
        "event_lines": 0,
        "plain_lines": 0,
        "step_events": 0,
        "tool_events": 0,
        "tool_failures": 0,
        "blocked_total": 0,
        "repeat_blocks": 0,
        "read_blocks": 0,
        "rewrite_refusals": 0,
        "guard_refreshes": 0,
        "supervisor_interventions": 0,
        "supervisor_false_claims": 0,
        "verifier_ran": 0,
        "verifier_auto_ran": 0,
        "verifier_final_ran": 0,
        "verifier_passed": 0,
        "verifier_failed": 0,
        "steps": None,
        "model_calls": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "final_ok": False,
        "final_text": "",
        "settled": "",
        "policy": "",
        "window": None,
        "session": "",
    }
    verdicts = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if not line.startswith("{"):
            counters["plain_lines"] += 1
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            counters["plain_lines"] += 1
            continue
        if not isinstance(payload, dict):
            counters["plain_lines"] += 1
            continue
        kind = payload.get("type")
        counters["event_lines"] += 1

        if kind == "start":
            counters["policy"] = str(payload.get("policy") or "")
            counters["window"] = payload.get("window")
            counters["session"] = str(payload.get("session") or "")
        elif kind == "step":
            counters["step_events"] += 1
        elif kind == "tool":
            counters["tool_events"] += 1
            name = str(payload.get("name") or "")
            ok = bool(payload.get("ok"))
            detail = str(payload.get("detail") or "")
            if not ok:
                counters["tool_failures"] += 1
            if name in VERIFIER_NAMES:
                counters["verifier_ran"] += 1
                if name == "自动验证":
                    counters["verifier_auto_ran"] += 1
                else:
                    counters["verifier_final_ran"] += 1
                verdicts.append(ok)
                counters["verifier_passed" if ok else "verifier_failed"] += 1
            if _blocked(detail):
                counters["blocked_total"] += 1
                if REFUSAL_MARKER in detail:
                    counters["rewrite_refusals"] += 1
                if IDENTICAL_RETRY_MARK in detail or REPEAT_GUARD_MARK in detail:
                    counters["repeat_blocks"] += 1
                if name in READ_TOOLS:
                    counters["read_blocks"] += 1
        elif kind == "note":
            note_kind = str(payload.get("kind") or "")
            note_text = str(payload.get("text") or "")
            if note_kind == GUARD_REFRESH_KIND:
                counters["guard_refreshes"] += 1
            if note_kind == FACT_CONFLICT_KIND:
                counters["supervisor_false_claims"] += 1
            if note_text.startswith(SUPERVISOR_PREFIX):
                counters["supervisor_interventions"] += 1
        elif kind == "usage":
            counters["steps"] = payload.get("steps")
            counters["model_calls"] = payload.get("calls")
            counters["prompt_tokens"] = payload.get("prompt_tokens")
            counters["completion_tokens"] = payload.get("completion_tokens")
        elif kind == "final":
            counters["final_ok"] = bool(payload.get("ok"))
            counters["final_text"] = str(payload.get("text") or "")
            counters["settled"] = str(payload.get("settled") or "")

    counters["verifier_ok"] = verdicts[-1] if verdicts else None
    return counters


# --------------------------------------------------------------------------
# 单次探针
# --------------------------------------------------------------------------


def run_check(python, script, sandbox, timeout):
    """跑验收判据。判据是标准库脚本，退出码 0 即通过。"""
    started = time.time()
    try:
        done = subprocess.run(
            [str(python), str(script), "--root", str(sandbox)],
            cwd=str(sandbox),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return {
            "exit_code": done.returncode,
            "output": (done.stdout or "") + (done.stderr or ""),
            "timed_out": False,
            "wall_seconds": round(time.time() - started, 3),
        }
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        return {
            "exit_code": None,
            "output": out + err,
            "timed_out": True,
            "wall_seconds": round(time.time() - started, 3),
        }


def resolve_options(manifest, args):
    """命令行 > 清单 defaults。缺项按内置默认补上。"""
    defaults = manifest["defaults"]
    extra = list(defaults.get("extra_args") or [])
    return {
        "provider": args.provider or defaults.get("provider") or "llamacpp",
        "base_url": args.base_url or defaults.get("base_url") or "",
        "model": args.model or defaults.get("model") or "",
        "session": args.session or defaults.get("session") or "probe",
        "scope": args.scope or defaults.get("scope") or "**",
        "window": args.window or int(defaults.get("window") or 0),
        "max_steps": args.max_steps or int(defaults.get("max_steps") or 8),
        "step_ceiling": args.step_ceiling
        or int(defaults.get("step_ceiling") or 24),
        "subagent_steps": args.subagent_steps
        or int(defaults.get("subagent_steps") or 20),
        "timeout": args.timeout or int(defaults.get("timeout_seconds") or 900),
        "check_timeout": int(defaults.get("check_timeout_seconds") or 300),
        "no_supervise": bool(args.no_supervise),
        "extra_args": extra,
        "spool": args.spool or "",
    }


def run_one(probe, manifest, options, root, runner=run_spool, python=None):
    """跑一个探针，返回一条记录。这是整个脚本的主干。"""
    python = Path(python or repo_python())
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    seed = (PROBES_DIR / probe["seed"]).resolve()
    sandbox = root / probe["id"]

    seed_before = fingerprint_tree(seed)
    materialize(seed, sandbox, root)
    policy_path = write_policy(sandbox)

    # 判据脚本：跑 check 时用它；验收脚本的路径在清单里写死，不改。
    check_script = (PROBES_DIR / probe["check"]["script"]).resolve()
    check_timeout = int(probe["check"].get("timeout_seconds") or options["check_timeout"])

    env = probe_env(sandbox)
    path_added = env.pop("_PROBE_PATH_ADDED", "")
    command = build_command(probe, sandbox, options)

    stdout_path = sandbox / ".agent" / "probe.stdout.jsonl"
    stderr_path = sandbox / ".agent" / "probe.stderr.txt"

    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    outcome = runner(
        command,
        cwd=sandbox,
        timeout=options["timeout"],
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        env=env,
    )

    stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.is_file() else ""
    stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.is_file() else ""
    counters = parse_events(stdout_text)

    result_files = fingerprint_tree(sandbox)
    seed_after = fingerprint_tree(seed)
    seed_unchanged = seed_before == seed_after
    change = diff_trees(seed_before, result_files)

    # 授权文件在跑完之后仍然无 BOM 且内容正确——这是「读工作区策略」那条路
    # 真的被走通的物证之一。
    policy_bytes = policy_path.read_bytes()
    policy_ok = (
        policy_bytes[:3] != b"\xef\xbb\xbf"
        and json.loads(policy_bytes.decode("utf-8")).get("policy") == "auto"
    )

    guard_failures = verify_guards(sandbox, probe.get("guard_hashes"))

    check = run_check(python, check_script, sandbox, check_timeout)
    check_ok = check["exit_code"] == 0 and not check["timed_out"] and not guard_failures

    record = {
        "id": probe["id"],
        "shape": probe["shape"],
        "title": probe.get("title", ""),
        "goal": probe["goal"],
        "started_at": started_at,
        "wall_seconds": outcome.get("wall_seconds", 0.0),
        "exit_code": outcome.get("exit_code"),
        "final_ok": bool(counters["final_ok"]),
        "steps": counters["steps"],
        "model_calls": counters["model_calls"],
        "prompt_tokens": counters["prompt_tokens"],
        "completion_tokens": counters["completion_tokens"],
        "verifier_ran": counters["verifier_ran"],
        "verifier_ok": counters["verifier_ok"],
        "repeat_blocks": counters["repeat_blocks"],
        "read_blocks": counters["read_blocks"],
        "supervisor_interventions": counters["supervisor_interventions"],
        "supervisor_false_claims": counters["supervisor_false_claims"],
        "file_changed": {
            "seed_digest": digest(seed_before),
            "result_digest": digest(result_files),
            "changed": bool(change["added"] or change["removed"] or change["modified"]),
            **change,
        },
        "check_ok": bool(check_ok),
        "notes": "",
        # ---- 额外读数：不是必填，但没有它们就分不清「模型不行」和「harness 不行」 ----
        "provider": options["provider"],
        "base_url": options["base_url"],
        "model": options["model"],
        "sandbox": str(sandbox),
        "seed_unchanged": bool(seed_unchanged),
        "policy_file_ok": bool(policy_ok),
        "policy_file": str(POLICY_RELATIVE).replace("\\", "/"),
        "guards_ok": not guard_failures,
        "guard_failures": guard_failures,
        "timed_out": bool(outcome.get("timed_out")),
        "spawn_error": outcome.get("spawn_error", ""),
        "tool_events": counters["tool_events"],
        "tool_failures": counters["tool_failures"],
        "blocked_total": counters["blocked_total"],
        "rewrite_refusals": counters["rewrite_refusals"],
        "guard_refreshes": counters["guard_refreshes"],
        "verifier_auto_ran": counters["verifier_auto_ran"],
        "verifier_final_ran": counters["verifier_final_ran"],
        "verifier_passed": counters["verifier_passed"],
        "verifier_failed": counters["verifier_failed"],
        "step_events": counters["step_events"],
        "event_lines": counters["event_lines"],
        "plain_lines": counters["plain_lines"],
        "policy_in_run": counters["policy"],
        "window": counters["window"],
        "settled": counters["settled"],
        "final_text": counters["final_text"],
        "check_exit_code": check["exit_code"],
        "check_timed_out": check["timed_out"],
        "check_wall_seconds": check["wall_seconds"],
        "check_output": check["output"][-2000:],
        "stderr_tail": stderr_text[-1000:],
        "events_path": str(stdout_path),
        "command": [str(item) for item in command],
        "python": str(python),
        "path_added": path_added,
        "timeout_seconds": options["timeout"],
        "max_steps": options["max_steps"],
        "step_ceiling": options["step_ceiling"],
        "bounds_ok": None,
        "bounds_failed": [],
    }

    bounds_failed = evaluate_bounds(probe, record)
    record["bounds_failed"] = bounds_failed
    record["bounds_ok"] = not bounds_failed
    record["notes"] = build_notes(record, probe)
    return record


def evaluate_bounds(probe, record):
    """对照清单里的「预期下限」。命中不了的写清楚，不吞。"""
    expect = probe.get("expect") or {}
    failed = []
    # 清单里的键（`max_*`）与记录里的键（裸计数）不是一个名字，这里显式对上。
    # 顺手写错过一次：直接拿 `max_read_blocks` 去 record 里取，取到 None，
    # 于是「拦读超上限」永远判成达标——单测把这条抓出来了。
    for record_key, expect_key, message in (
        ("repeat_blocks", "max_repeat_blocks", "原样重试被拦次数超上限"),
        ("read_blocks", "max_read_blocks", "读取被拦次数超上限"),
        (
            "supervisor_false_claims",
            "max_supervisor_false_claims",
            "督导说错事实的次数超上限",
        ),
    ):
        if expect_key not in expect:
            continue
        limit = expect[expect_key]
        value = record.get(record_key, 0)
        if value > limit:
            failed.append(f"{message}：{value} > {limit}")
    if record.get("timed_out"):
        failed.append(f"探针超时（{record.get('timeout_seconds')}s）被强杀")
    if record.get("spawn_error"):
        failed.append(f"spool 没能启动：{record['spawn_error']}")
    if not record.get("seed_unchanged", True):
        failed.append("种子目录被污染了")
    if not record.get("policy_file_ok", True):
        failed.append("工作区授权文件不合法（BOM 或内容不对）")
    # `start` 事件里报的策略必须就是工作区里那一份。BOM 会让 load_policy 静默退回
    # `ask`，症状是「每步写操作都要确认」，而这里能直接读出真相。
    if record.get("policy_in_run") not in ("", "auto"):
        failed.append(
            f"本次运行的策略是 {record['policy_in_run']!r}，不是工作区里写的 auto"
        )
    if not record.get("guards_ok", True):
        failed.extend(record.get("guard_failures") or ["受保护文件被改过"])
    return failed


def build_notes(record, probe):
    """人读的一行。把「谁不行」说清楚：模型没做完 / harness 拦住了 / 判据没过。"""
    parts = []
    if record["timed_out"]:
        parts.append(f"超时被强杀（{record['timeout_seconds']}s）")
    if record["spawn_error"]:
        parts.append(f"没能启动：{record['spawn_error'][:120]}")
    parts.append("任务自认完成" if record["final_ok"] else "任务自认未完成")
    parts.append("验收绿" if record["check_ok"] else "验收红")
    if record["verifier_ran"]:
        parts.append(
            f"自动验证 {record['verifier_passed']}/{record['verifier_ran']} 次通过"
        )
    else:
        parts.append("自动验证一次都没跑")
    if record["repeat_blocks"]:
        parts.append(f"原样重试被拦 {record['repeat_blocks']} 次")
    if record["read_blocks"]:
        parts.append(f"读取被拦 {record['read_blocks']} 次（可能死锁）")
    if record["rewrite_refusals"]:
        parts.append(f"整文件重写被拒 {record['rewrite_refusals']} 次")
    if record["supervisor_interventions"]:
        parts.append(f"督导介入 {record['supervisor_interventions']} 次")
    if record["supervisor_false_claims"]:
        parts.append(f"督导说错事实 {record['supervisor_false_claims']} 次")
    if not record["file_changed"]["changed"]:
        parts.append("一个文件都没改")
    if record["bounds_failed"]:
        parts.append("下限未达标：" + "；".join(record["bounds_failed"]))
    else:
        parts.append("下限全达标")
    if record["plain_lines"]:
        parts.append(f"stdout 有 {record['plain_lines']} 行非事件输出")
    return "；".join(parts)


# --------------------------------------------------------------------------
# 落盘与展示
# --------------------------------------------------------------------------


def save_record(record, stamp):
    RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    path = RECORDS_DIR / f"{stamp}-{record['id']}.json"
    path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with RESULTS_JSONL.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def save_summary(records, meta):
    payload = {"meta": meta, "probes": records}
    SUMMARY_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return SUMMARY_PATH


def _cell(value, width=None):
    text = "" if value is None else str(value)
    if width:
        text = text[:width]
    return text


def render_table(records):
    """一行一个探针的汇总表。列就是报告里要念的那些数字。"""
    header = (
        f"{'探针':<18}{'形状':<18}{'结局':<10}{'步':>3}{'调用':>5}"
        f"{'输入tok':>9}{'输出tok':>9}{'墙钟s':>8}{'验证器':>8}"
        f"{'重试':>5}{'拦读':>5}{'督导':>5}{'督导错':>7}{'判据':>6}{'下限':>6}"
    )
    lines = [header, "-" * len(header)]
    for record in records:
        if record.get("skipped"):
            lines.append(
                f"{record['id']:<18}{_cell(record.get('shape'), 18):<18}"
                f"{'跳过':<10}{'—':>3}{'—':>5}{'—':>9}{'—':>9}"
                f"{record.get('wall_seconds', 0):>8.1f}{'—':>8}{'—':>5}{'—':>5}"
                f"{'—':>5}{'—':>7}{'—':>6}{'—':>6}"
            )
            continue
        outcome = ("完成" if record["final_ok"] else "未完成") + (
            "/绿" if record["check_ok"] else "/红"
        )
        verifier = (
            f"{record['verifier_passed']}/{record['verifier_ran']}"
            if record["verifier_ran"]
            else "未跑"
        )
        lines.append(
            f"{record['id']:<18}{_cell(record['shape'], 18):<18}{outcome:<10}"
            f"{_cell(record['steps'], 3):>3}{_cell(record['model_calls'], 5):>5}"
            f"{_cell(record['prompt_tokens'], 9):>9}"
            f"{_cell(record['completion_tokens'], 9):>9}"
            f"{record['wall_seconds']:>8.1f}{verifier:>8}"
            f"{record['repeat_blocks']:>5}{record['read_blocks']:>5}"
            f"{record['supervisor_interventions']:>5}"
            f"{record['supervisor_false_claims']:>7}"
            f"{'绿' if record['check_ok'] else '红':>6}"
            f"{'过' if record['bounds_ok'] else '未过':>6}"
        )
    return "\n".join(lines)


def render_listing(manifest):
    lines = [f"共 {len(manifest['probes'])} 个探针（{MANIFEST_PATH}）", ""]
    for probe in manifest["probes"]:
        lines.append(f"[{probe['id']}] 形状 {probe['shape']}")
        lines.append(f"  种子：{probe['seed']}    判据：{probe['check']['script']}")
        lines.append(f"  用户原话：{probe['goal']}")
        for item in probe.get("lower_bounds") or []:
            lines.append(f"    · {item}")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        description="真实任务能力探针的跑分器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--list", action="store_true", help="列出全部探针")
    parser.add_argument("--probe", default="", help="只跑这一个探针（id）")
    parser.add_argument("--all", action="store_true", help="跑全部探针并出汇总表")
    parser.add_argument("--manifest", default=str(MANIFEST_PATH), help="探针清单路径")
    parser.add_argument(
        "--root",
        default="",
        help="沙箱根目录；每个探针跑在 <root>/<id>。不给就用系统临时目录",
    )
    parser.add_argument("--provider", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--spool", default="", help="spool 可执行文件；默认用仓库 venv 里的")
    parser.add_argument("--session", default="")
    parser.add_argument("--scope", default="")
    parser.add_argument("--window", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=0, help="单个探针的墙钟上限（秒）")
    parser.add_argument("--max-steps", type=int, default=0)
    parser.add_argument("--step-ceiling", type=int, default=0)
    parser.add_argument("--subagent-steps", type=int, default=0)
    parser.add_argument("--no-supervise", action="store_true")
    parser.add_argument(
        "--total-budget",
        type=int,
        default=0,
        help="--all 的总墙钟上限（秒）；超预算的探针记为跳过，不偷偷跑下去",
    )
    parser.add_argument(
        "--no-save", action="store_true", help="只打印，不落记录（自检/单测用）"
    )
    parser.add_argument(
        "--report",
        default="",
        help="把汇总表与合计另存一份 UTF-8 文本（Windows 控制台是 cp936，"
        "中文会糊成乱码；要贴进报告就读这份文件）",
    )
    return parser


def main(argv=None, runner=run_spool):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
    except ManifestError as exc:
        print(f"探针清单有问题：{exc}", file=sys.stderr)
        return 2

    if args.list:
        print(render_listing(manifest))
        return 0
    if not args.all and not args.probe:
        parser.print_help()
        return 2

    options = resolve_options(manifest, args)
    root = Path(args.root).expanduser().resolve() if args.root else default_root()
    budget = args.total_budget or int(
        manifest["defaults"].get("total_budget_seconds") or 0
    )
    stamp = time.strftime("%Y%m%d-%H%M%S")
    python = repo_python()

    if args.probe:
        probe = find_probe(manifest, args.probe)
        selected = [probe]
    else:
        selected = list(manifest["probes"])

    print(f"沙箱根：{root}")
    print(
        f"供应商：{options['provider']}"
        + (f"（{options['base_url']}）" if options["base_url"] else "")
        + f"；单探针超时 {options['timeout']}s；最多 {options['max_steps']} 步"
        + (f"；整批预算 {budget}s" if budget else "")
    )
    print(f"解释器：{python}")
    print("")

    records = []
    batch_started = time.time()
    for probe in selected:
        elapsed = time.time() - batch_started
        remaining = budget - elapsed if budget else None
        if remaining is not None and remaining < 60:
            record = {
                "id": probe["id"],
                "shape": probe["shape"],
                "goal": probe["goal"],
                "skipped": True,
                "notes": f"整批预算 {budget}s 不够了（已用 {elapsed:.0f}s），这一条没跑",
                "wall_seconds": 0.0,
                "final_ok": False,
                "check_ok": False,
                "bounds_ok": False,
                "steps": None,
                "model_calls": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "verifier_ran": 0,
                "verifier_ok": None,
                "repeat_blocks": 0,
                "read_blocks": 0,
                "supervisor_interventions": 0,
                "supervisor_false_claims": 0,
                "file_changed": {"changed": False},
                "exit_code": None,
                "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            records.append(record)
            print(f"[{probe['id']}] 跳过：整批预算不足")
            continue

        # 单条也不许把整批预算冲破：取「本探针上限」与「剩余预算」里更小的那个。
        local = dict(options)
        if remaining is not None:
            local["timeout"] = int(max(60, min(options["timeout"], remaining)))
        print(f"[{probe['id']}] 开跑（{probe['shape']}）…")
        record = run_one(probe, manifest, local, root, runner=runner, python=python)
        records.append(record)
        print(
            f"[{probe['id']}] exit={record['exit_code']} "
            f"步={record['steps']} 墙钟={record['wall_seconds']:.1f}s "
            f"验收={'绿' if record['check_ok'] else '红'} 下限={'过' if record['bounds_ok'] else '未过'}"
        )
        if not args.no_save:
            save_record(record, stamp)

    total_wall = round(time.time() - batch_started, 1)
    table = render_table(records)
    totals = {
        "probes": len(records),
        "finished": sum(1 for item in records if item.get("final_ok")),
        "check_ok": sum(1 for item in records if item.get("check_ok")),
        "bounds_ok": sum(1 for item in records if item.get("bounds_ok")),
        "skipped": sum(1 for item in records if item.get("skipped")),
        "steps": sum(item.get("steps") or 0 for item in records),
        "model_calls": sum(item.get("model_calls") or 0 for item in records),
        "prompt_tokens": sum(item.get("prompt_tokens") or 0 for item in records),
        "completion_tokens": sum(item.get("completion_tokens") or 0 for item in records),
    }
    summary_line = (
        "合计：完成 {finished}/{probes}，验收绿 {check_ok}/{probes}，"
        "下限达标 {bounds_ok}/{probes}，步数 {steps}，模型调用 {model_calls}，"
        "token 输入 {prompt_tokens} / 输出 {completion_tokens}".format(**totals)
    )
    wall_line = f"整批墙钟：{total_wall}s" + (f"（预算上限 {budget}s）" if budget else "")

    print("")
    print(table)
    print("")
    print(wall_line)
    print(summary_line)

    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            "\n".join(
                [
                    f"供应商：{options['provider']}"
                    + (f"（{options['base_url']}）" if options["base_url"] else ""),
                    f"沙箱根：{root}",
                    wall_line,
                    "",
                    table,
                    "",
                    summary_line,
                    "",
                    "逐条说明：",
                    *[f"  {item['id']}：{item.get('notes', '')}" for item in records],
                    "",
                ]
            ),
            encoding="utf-8",
            newline="\n",
        )
        print(f"文本报告：{report_path}")

    if not args.no_save:
        meta = {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "provider": options["provider"],
            "base_url": options["base_url"],
            "model": options["model"],
            "root": str(root),
            "python": str(python),
            "total_wall_seconds": total_wall,
            "budget_seconds": budget,
            "within_budget": (not budget) or total_wall <= budget,
            "totals": totals,
        }
        if args.all:
            path = save_summary(records, meta)
            print(f"汇总已落盘：{path}")
        print(f"逐条记录目录：{RECORDS_DIR}")
        print(f"追加表：{RESULTS_JSONL}")

    return 0 if all(
        item.get("check_ok") and item.get("bounds_ok") for item in records
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
