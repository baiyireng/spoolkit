"""探针跑分器的单测。

**这里一次模型都不调。** 需要 spool 子进程的地方一律用假 runner（只写一份预设的
事件流、顺手在沙箱里改个文件），因为单测要测的是跑分器自己的三条不变式：

1. 清单格式合法、五种任务形状齐全；
2. 沙箱复制不污染种子，授权文件无 BOM；
3. 记录字段齐全、下限判定真的会红。

外加一条**赛道上最重要**的：每个探针的判据在未修改的种子上是红的、在人工修好的
版本上是绿的——做不到这一点的探针不许进清单，所以它必须有测试钉着。
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import probe as probe_runner  # noqa: E402

MANIFEST = probe_runner.load_manifest()
PROBES_DIR = probe_runner.PROBES_DIR


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


def probe_by_id(identifier):
    return probe_runner.find_probe(MANIFEST, identifier)


def copy_onto(src, dst):
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    return dst


def run_check(script, sandbox, timeout=300):
    done = subprocess.run(
        [sys.executable, str(script), "--root", str(sandbox)],
        cwd=str(sandbox),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return done.returncode, (done.stdout or "") + (done.stderr or "")


CANNED_EVENTS = "\n".join(
    [
        json.dumps(
            {
                "type": "start",
                "session": "probe",
                "goal": "g",
                "policy": "auto",
                "window": 8192,
            },
            ensure_ascii=False,
        ),
        json.dumps({"type": "step", "n": 1}, ensure_ascii=False),
        # 一次原样重试被硬拦
        json.dumps(
            {
                "type": "tool",
                "name": "write_file",
                "ok": False,
                "detail": "⛔ 未执行：这个调用与上一次**完全相同**（工具 write_file，参数一字未改）",
            },
            ensure_ascii=False,
        ),
        # 一次读取被拦（死锁的形状）
        json.dumps(
            {
                "type": "tool",
                "name": "read_file",
                "ok": False,
                "detail": "⛔ 未执行：这个调用在最近几步里已经做过 2 次，结果不会改变",
            },
            ensure_ascii=False,
        ),
        # 一次整文件重写被拒（砍半护栏）
        json.dumps(
            {
                "type": "tool",
                "name": "write_file",
                "ok": False,
                "detail": "⚠ 拒绝整文件重写：index.html 从 40 行变成 180 行，改动幅度超过阈值",
            },
            ensure_ascii=False,
        ),
        # 护栏放行了一次刷新读取
        json.dumps(
            {"type": "note", "text": "⟳ 刷新读取：文件已经变了", "ok": True, "kind": "guard_refresh"},
            ensure_ascii=False,
        ),
        # 督导介入，而且被核出事实不符
        json.dumps({"type": "note", "text": "督导：续 6 步——还有活没干", "ok": True}, ensure_ascii=False),
        json.dumps(
            {
                "type": "note",
                "text": "督导的断言与记录不符，已按记录改写：0 处改动 → 3 处改动",
                "ok": False,
                "kind": "supervisor_fact_conflict",
            },
            ensure_ascii=False,
        ),
        # 自动验证跑了两次，一红一绿
        json.dumps({"type": "tool", "name": "自动验证", "ok": False, "detail": "FAILED"}, ensure_ascii=False),
        json.dumps({"type": "tool", "name": "自动验证", "ok": True, "detail": "通过"}, ensure_ascii=False),
        json.dumps(
            {
                "type": "usage",
                "steps": 5,
                "calls": 7,
                "prompt_tokens": 1234,
                "completion_tokens": 567,
            },
            ensure_ascii=False,
        ),
        json.dumps(
            {"type": "final", "ok": True, "text": "做完了", "settled": "auto"},
            ensure_ascii=False,
        ),
        "这是一行散文，混进了 stdout",
    ]
)


def fake_runner_factory(captured):
    """假 runner：写预设事件流、在沙箱里改一个文件、顺手记下命令行。"""

    def fake_runner(command, *, cwd, timeout, stdout_path, stderr_path, env):
        captured.append(
            {
                "command": [str(item) for item in command],
                "cwd": str(cwd),
                "timeout": timeout,
                "env": env,
            }
        )
        Path(stdout_path).write_text(CANNED_EVENTS + "\n", encoding="utf-8", newline="\n")
        Path(stderr_path).write_text("", encoding="utf-8")
        # 制造一处改动，让 file_changed 有东西可读
        target = Path(cwd) / "README.md"
        if target.is_file():
            target.write_text(
                target.read_text(encoding="utf-8") + "\n（探针假 runner 改过）\n",
                encoding="utf-8",
                newline="\n",
            )
        return {
            "exit_code": 1,
            "timed_out": False,
            "spawn_error": "",
            "wall_seconds": 0.5,
        }

    return fake_runner


# --------------------------------------------------------------------------
# 清单
# --------------------------------------------------------------------------


def test_清单合法且五种形状齐全():
    ids = [item["id"] for item in MANIFEST["probes"]]
    assert len(ids) == len(set(ids)), f"探针 id 有重复：{ids}"
    shapes = {item["shape"] for item in MANIFEST["probes"]}
    assert set(probe_runner.REQUIRED_SHAPES) <= shapes, (
        f"缺任务形状：{set(probe_runner.REQUIRED_SHAPES) - shapes}"
    )
    assert len(MANIFEST["probes"]) >= 5
    for item in MANIFEST["probes"]:
        # 用户原话不许被改清楚：它必须是一句像个用户会说的话，不是一份规格书
        assert len(item["goal"]) >= 12, item["id"]
        assert (PROBES_DIR / item["seed"]).is_dir()
        assert (PROBES_DIR / item["expected"]).is_dir()
        assert item["lower_bounds"], f"{item['id']} 没写预期下限"


def test_清单缺字段要报错(tmp_path):
    bad = {
        "version": 1,
        "probes": [
            {"id": "x", "shape": "add-feature", "title": "t", "seed": "seeds/add-feature"}
        ],
    }
    path = tmp_path / "probes.json"
    path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(probe_runner.ManifestError):
        probe_runner.load_manifest(path)


def test_清单里的guard_hash与种子逐字相符():
    """guard_hashes 是手抄进来的哈希，抄错一位不会报错、只会让每次都判「被改过」。

    手抄真的错过一次（少了一位，长度 63 而不是 64），症状是 fix-bug 永远红。
    所以这条测试直接拿种子重算一遍。
    """
    import hashlib

    checked = 0
    for item in MANIFEST["probes"]:
        for rel, want in (item.get("guard_hashes") or {}).items():
            target = PROBES_DIR / item["seed"] / rel
            assert target.is_file(), f"{item['id']} 钉住的文件不在种子里：{rel}"
            got = hashlib.sha256(target.read_bytes()).hexdigest()
            assert got == want, f"{item['id']} 的 {rel} 哈希对不上：{got} != {want}"
            checked += 1
    assert checked >= 1, "至少要有探针用 guard_hashes 钉住不该改的文件"


def test_种子与期望版本里没有字节码缓存():
    """种子目录是**只读的原料**，不该有 `.pyc`。

    有的话只有一种解释：有人直接在种子目录里跑过 python / pytest。那既污染了
    种子，也让 `file_changed` 一上来就带着噪声（第一版真的这样：我手工冒烟时
    在种子目录里跑了 unittest，于是五个种子里都长出了 `__pycache__`）。
    """
    dirty = []
    for kind in ("seeds", "expected"):
        for path in sorted((PROBES_DIR / kind).rglob("*")):
            if not path.is_file():
                continue
            parts = path.relative_to(PROBES_DIR / kind).parts
            if any(part in probe_runner.CACHE_DIRS for part in parts) or path.suffix == ".pyc":
                dirty.append(str(path.relative_to(PROBES_DIR)))
    assert not dirty, f"种子/期望版本里有缓存痕迹：{dirty}"


def test_指纹跳过缓存目录(tmp_path):
    root = tmp_path / "t"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (root / "pkg" / "__pycache__").mkdir()
    (root / "pkg" / "__pycache__" / "a.cpython-314.pyc").write_bytes(b"\x00")
    (root / "orphan.pyc").write_bytes(b"\x00")
    files = probe_runner.fingerprint_tree(root)
    assert list(files) == ["pkg/a.py"]


def test_清单里不存在的种子要报错(tmp_path):
    bad = {
        "version": 1,
        "probes": [
            {
                "id": "x",
                "shape": "add-feature",
                "title": "t",
                "seed": "seeds/根本没有这个",
                "expected": "expected/add-feature",
                "goal": "随便一句话当作目标",
                "check": {"script": "checks/add_feature.py"},
            }
        ],
    }
    path = tmp_path / "probes.json"
    path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(probe_runner.ManifestError):
        probe_runner.load_manifest(path)


# --------------------------------------------------------------------------
# 判据必须能证伪：种子红 / 修好绿
# --------------------------------------------------------------------------


@pytest.mark.parametrize("identifier", [item["id"] for item in MANIFEST["probes"]])
def test_判据在种子上是红的(identifier, tmp_path):
    probe = probe_by_id(identifier)
    sandbox = copy_onto(PROBES_DIR / probe["seed"], tmp_path / identifier)
    script = PROBES_DIR / probe["check"]["script"]
    code, output = run_check(script, sandbox)
    assert code != 0, f"{identifier} 的判据在未修改的种子上居然是绿的：\n{output[-1500:]}"


@pytest.mark.parametrize("identifier", [item["id"] for item in MANIFEST["probes"]])
def test_判据在人工修好的版本上是绿的(identifier, tmp_path):
    probe = probe_by_id(identifier)
    sandbox = copy_onto(PROBES_DIR / probe["expected"], tmp_path / identifier)
    script = PROBES_DIR / probe["check"]["script"]
    code, output = run_check(script, sandbox)
    assert code == 0, f"{identifier} 的判据在修好的版本上仍然是红的：\n{output[-2000:]}"


# --------------------------------------------------------------------------
# 沙箱与授权
# --------------------------------------------------------------------------


def test_沙箱复制不污染种子(tmp_path):
    probe = probe_by_id("rename-cross-file")
    seed = PROBES_DIR / probe["seed"]
    before = probe_runner.fingerprint_tree(seed)

    root = tmp_path / "runs"
    sandbox = probe_runner.materialize(seed, root / probe["id"], root)
    assert (sandbox / "pipeline" / "fetch.py").is_file()

    # 在副本里乱改一通，种子必须一个字节都不动
    (sandbox / "pipeline" / "fetch.py").write_text("彻底改掉\n", encoding="utf-8")
    (sandbox / "新文件.txt").write_text("新增\n", encoding="utf-8")
    shutil.rmtree(sandbox / "docs")

    after = probe_runner.fingerprint_tree(seed)
    assert before == after, "种子目录被沙箱里的改动污染了"


def test_授权文件无BOM且读得出auto(tmp_path):
    root = tmp_path / "runs"
    root.mkdir()
    sandbox = root / "x"
    sandbox.mkdir()
    path = probe_runner.write_policy(sandbox)

    raw = path.read_bytes()
    assert raw[:3] != b"\xef\xbb\xbf", "授权文件被写成了带 BOM 的 UTF-8"
    assert json.loads(raw.decode("utf-8"))["policy"] == "auto"
    assert path.parent.name == ".agent"

    # 说明这条不变式为什么重要：BOM 会让 `load_policy` 那条读法直接失败，
    # 而它只 catch JSONDecodeError 然后**静默退回 ask** —— 探针就会每步都要确认。
    # 注意要按 `load_policy` 的读法复现（先 utf-8 解码成 str 再 json.loads）：
    # 直接把 bytes 交给 json.loads，它会自动识别 utf-8-sig，看不出问题。
    with pytest.raises(json.JSONDecodeError):
        json.loads(b"\xef\xbb\xbf".decode("utf-8") + raw.decode("utf-8"))


def test_沙箱必须落在root的直接子目录(tmp_path):
    probe = probe_by_id("fix-bug")
    seed = PROBES_DIR / probe["seed"]
    root = tmp_path / "runs"
    root.mkdir()
    with pytest.raises(ValueError):
        probe_runner.materialize(seed, root / "a" / "b", root)
    with pytest.raises(ValueError):
        probe_runner.materialize(seed, REPO_ROOT / "benchmarks" / "probes" / "tmp-x", root)


# --------------------------------------------------------------------------
# 事件解析
# --------------------------------------------------------------------------


def test_事件解析数得清各类拦截与督导():
    counters = probe_runner.parse_events(CANNED_EVENTS)
    assert counters["policy"] == "auto"
    assert counters["steps"] == 5
    assert counters["model_calls"] == 7
    assert counters["prompt_tokens"] == 1234
    assert counters["completion_tokens"] == 567
    assert counters["final_ok"] is True
    assert counters["blocked_total"] == 3
    assert counters["repeat_blocks"] == 2, "原样重试与重复护栏都算「原样重试被拦」"
    assert counters["read_blocks"] == 1
    assert counters["rewrite_refusals"] == 1
    assert counters["guard_refreshes"] == 1
    assert counters["supervisor_interventions"] == 1
    assert counters["supervisor_false_claims"] == 1
    assert counters["verifier_ran"] == 2
    assert counters["verifier_passed"] == 1
    assert counters["verifier_failed"] == 1
    assert counters["verifier_ok"] is True, "verifier_ok 取最后一次判定"
    assert counters["plain_lines"] == 1, "散文行要单独数出来"


# --------------------------------------------------------------------------
# 记录与下限
# --------------------------------------------------------------------------


def test_记录字段齐全且不改种子(tmp_path):
    probe = probe_by_id("merge-html")
    seed = PROBES_DIR / probe["seed"]
    seed_before = probe_runner.fingerprint_tree(seed)

    captured = []
    options = probe_runner.resolve_options(
        MANIFEST,
        probe_runner.build_parser().parse_args(
            ["--probe", "merge-html", "--root", str(tmp_path)]
        ),
    )
    record = probe_runner.run_one(
        probe,
        MANIFEST,
        options,
        tmp_path,
        runner=fake_runner_factory(captured),
        python=sys.executable,
    )

    for key in probe_runner.REQUIRED_RECORD_KEYS:
        assert key in record, f"记录缺字段 {key}"
    assert record["id"] == "merge-html"
    assert record["goal"] == probe["goal"]
    assert record["started_at"]
    assert record["wall_seconds"] == 0.5
    assert record["exit_code"] == 1
    assert record["final_ok"] is True
    assert record["steps"] == 5
    assert record["model_calls"] == 7
    assert record["prompt_tokens"] == 1234
    assert record["completion_tokens"] == 567
    assert record["verifier_ran"] == 2
    assert record["verifier_ok"] is True, "verifier_ok 取最后一次判定"
    assert record["repeat_blocks"] == 2
    assert record["read_blocks"] == 1
    assert record["supervisor_interventions"] == 1
    assert record["supervisor_false_claims"] == 1
    assert record["notes"]

    changed = record["file_changed"]
    assert changed["changed"] is True
    assert "README.md" in changed["modified"]
    assert changed["seed_digest"] != changed["result_digest"]

    assert record["seed_unchanged"] is True
    assert probe_runner.fingerprint_tree(seed) == seed_before
    assert record["policy_file_ok"] is True
    assert record["policy_in_run"] == "auto"
    # 假 runner 什么都没修好，验收必须是红的，下限也必须是「未过」
    assert record["check_ok"] is False
    assert record["bounds_ok"] is False
    assert any("原样重试" in item for item in record["bounds_failed"])


def test_命令行不传policy且真的走events(tmp_path):
    captured = []
    code = probe_runner.main(
        ["--probe", "merge-html", "--root", str(tmp_path), "--no-save"],
        runner=fake_runner_factory(captured),
    )
    assert code == 1, "假 runner 什么都没修好，退出码应当是 1"
    assert len(captured) == 1
    command = captured[0]["command"]
    assert "--events" in command
    assert "--policy" not in command, "授权必须走工作区 policy.json，不能从命令行给"
    assert command[command.index("--scope") + 1] == "**"
    assert command[command.index("--goal") + 1] == probe_by_id("merge-html")["goal"]
    assert "--no-setup" in command
    # 探针的环境必须与开发机隔离
    env = captured[0]["env"]
    assert Path(env["SPOOLKIT_STATE"]).is_relative_to(tmp_path)
    assert Path(env["SPOOLKIT_CONFIG"]).is_relative_to(tmp_path)
    assert Path(env["SPOOLKIT_ENV_FILE"]).is_relative_to(tmp_path)
    assert captured[0]["timeout"] > 0


def test_环境补上rg与node(tmp_path):
    env = probe_runner.probe_env(tmp_path)
    import shutil as _shutil

    # 说明：这条不是断言一定找得到，而是断言「找不到时探针不会假装找得到」——
    # 补 PATH 的结果写在 _PROBE_PATH_ADDED 里，由 probe_env 弹出去进记录。
    assert "PATH" in env
    if _shutil.which("rg", path=env["PATH"]) is None:
        assert "rg" in env["_PROBE_PATH_ADDED"] or env["_PROBE_PATH_ADDED"] == ""


def test_下限判定会红():
    probe = probe_by_id("fix-bug")
    base = {
        "repeat_blocks": 0,
        "read_blocks": 0,
        "supervisor_false_claims": 0,
        "timed_out": False,
        "spawn_error": "",
        "seed_unchanged": True,
        "policy_file_ok": True,
        "guards_ok": True,
        "guard_failures": [],
        "policy_in_run": "auto",
    }
    assert probe_runner.evaluate_bounds(probe, dict(base)) == []

    dirty = dict(base, read_blocks=1)
    failed = probe_runner.evaluate_bounds(probe, dirty)
    assert any("读取被拦" in item for item in failed)

    timed = dict(base, timed_out=True, timeout_seconds=900)
    assert any("超时" in item for item in probe_runner.evaluate_bounds(probe, timed))

    guarded = dict(base, guards_ok=False, guard_failures=["tests/test_merge.py 被改过"])
    assert any("被改过" in item for item in probe_runner.evaluate_bounds(probe, guarded))

    wrong_policy = dict(base, policy_in_run="ask")
    assert any("auto" in item for item in probe_runner.evaluate_bounds(probe, wrong_policy))


def test_汇总表把一条跳过也画得出来():
    record = {
        "id": "x",
        "shape": "fix-bug",
        "skipped": True,
        "wall_seconds": 0.0,
        "notes": "预算不够",
    }
    table = probe_runner.render_table([record])
    assert "跳过" in table
    assert "x" in table
