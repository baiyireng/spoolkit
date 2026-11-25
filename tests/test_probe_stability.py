"""探针跑分器的「带扰动的重复」与聚合的单测。

**这里一次模型都不调。** 与 `tests/test_probe_runner.py` 同一套路：需要 spool
子进程的地方一律用假 runner（只写一份预设事件流、顺手改个文件）。

这一层要钉住的是四件事，每一件都能证伪：

1. **聚合口径**：通过率 = 「判据绿 且 下限过」/ 真跑了的次数。把失败当成功、
   把跳过算进分母、把 None 当 0 —— 三种写错都必须让某条测试变红。
2. **记录字段**：temperature / seed / 第几次 / 变体都要落进记录与 JSONL，
   否则「这一批是什么设置下取的」就无从复原。
3. **变体只换说法**：轮换的是 `--goal`，判据路径与 guard 哈希一个字没动；
   没给变体（或 `--no-variants`）时必须退化成固定目标。
4. **有界**：预算不够时先减少重复次数，并把「少采了几次」如实记下来。
"""

import json
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import probe as probe_runner  # noqa: E402

MANIFEST = probe_runner.load_manifest()
PROBES_DIR = probe_runner.PROBES_DIR

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
        json.dumps(
            {
                "type": "usage",
                "steps": 4,
                "calls": 6,
                "prompt_tokens": 900,
                "completion_tokens": 300,
            },
            ensure_ascii=False,
        ),
        json.dumps(
            {"type": "final", "ok": True, "text": "做完了", "settled": "auto"},
            ensure_ascii=False,
        ),
    ]
)


def fake_runner_factory(captured, sleep=0.0):
    """假 runner：写预设事件流、记下命令行。返回的墙钟固定 1.5s。

    `sleep` 用来让「预算按每一次重复检查」这件事可判定：真跑一次要花时间，
    预算才会真的变少。
    """

    def fake_runner(command, *, cwd, timeout, stdout_path, stderr_path, env):
        captured.append({"command": [str(item) for item in command], "timeout": timeout})
        if sleep:
            time.sleep(sleep)
        Path(stdout_path).write_text(CANNED_EVENTS + "\n", encoding="utf-8", newline="\n")
        Path(stderr_path).write_text("", encoding="utf-8")
        return {
            "exit_code": 0,
            "timed_out": False,
            "spawn_error": "",
            "wall_seconds": 1.5,
        }

    return fake_runner


def synthetic(
    identifier="probe-x",
    *,
    check_ok=True,
    bounds_ok=True,
    steps=4,
    wall_seconds=10.0,
    temperature=0.4,
    seed=1,
    repeat_index=0,
    variant_index=None,
    variant_source="goal",
    skipped=False,
    prompt_tokens=1000,
    completion_tokens=200,
    model_calls=5,
):
    """一条合成的记录。字段名与 run_one 产出的记录一致。"""
    return {
        "id": identifier,
        "shape": "fix-bug",
        "goal": "合成目标",
        "goal_base": "合成目标",
        "temperature": temperature,
        "seed": seed,
        "repeat_index": repeat_index,
        "repeat_total": 3,
        "variant_index": variant_index,
        "variant_source": variant_source,
        "check_ok": check_ok,
        "bounds_ok": bounds_ok,
        "final_ok": True,
        "steps": steps,
        "model_calls": model_calls,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "wall_seconds": wall_seconds,
        "timed_out": False,
        "notes": "合成",
        "sandbox": f"/tmp/{identifier}-r{repeat_index}",
        "events_path": f"/tmp/{identifier}-r{repeat_index}/.agent/probe.stdout.jsonl",
        "check_output": "",
        "skipped": skipped,
    }


def probe_by_id(identifier):
    return probe_runner.find_probe(MANIFEST, identifier)


def options_for(argv, manifest=MANIFEST):
    return probe_runner.resolve_options(
        manifest, probe_runner.build_parser().parse_args(argv)
    )


# --------------------------------------------------------------------------
# 聚合口径
# --------------------------------------------------------------------------


def test_通过率只数判据绿且下限过():
    """把「判据绿」或「下限过」单独当成过，都会让这条红。"""
    records = [
        synthetic("a", check_ok=True, bounds_ok=True),
        synthetic("a", check_ok=True, bounds_ok=False),
        synthetic("a", check_ok=False, bounds_ok=True),
        synthetic("a", check_ok=False, bounds_ok=False),
    ]
    item = probe_runner.aggregate_stability(records)["a"]
    assert item["attempts"] == 4
    assert item["passed"] == 1, "只有「判据绿 且 下限过」才算过"
    assert item["checks_green"] == 2
    assert item["bounds_ok"] == 2
    assert item["pass_rate"] == pytest.approx(0.25)
    assert item["unstable"] is True
    assert len(item["failures"]) == 3


def test_通过率全绿才算稳定():
    item = probe_runner.aggregate_stability(
        [synthetic("a"), synthetic("a", repeat_index=1)]
    )["a"]
    assert item["pass_rate"] == 1.0
    assert item["unstable"] is False
    assert item["failures"] == []


def test_极差与均值只算报出数字的那几次():
    """超时/没跑完的那一次没报出步数，把它当 0 会让极差凭空变大。"""
    records = [
        synthetic("a", steps=3, wall_seconds=10.0, prompt_tokens=100),
        synthetic("a", steps=5, wall_seconds=20.0, prompt_tokens=300),
        synthetic("a", steps=None, wall_seconds=None, prompt_tokens=None),
    ]
    item = probe_runner.aggregate_stability(records)["a"]
    assert item["metrics"]["steps"] == {
        "count": 2,
        "min": 3.0,
        "max": 5.0,
        "mean": 4.0,
        "span": 2.0,
    }
    assert item["metrics"]["wall_seconds"]["span"] == 10.0
    assert item["metrics"]["prompt_tokens"]["mean"] == 200.0
    # 三次尝试都过，但有两次没报数字——极差只对那两次负责
    assert item["passed"] == 3
    assert item["pass_rate"] == 1.0


def test_单次采样时极差为零而不是没有():
    item = probe_runner.aggregate_stability([synthetic("a", steps=7)])["a"]
    metric = item["metrics"]["steps"]
    assert metric["count"] == 1
    assert metric["min"] == metric["max"] == metric["mean"] == 7.0
    assert metric["span"] == 0.0


def test_跳过的记录既不算分子也不算分母():
    records = [
        synthetic("a"),
        synthetic("a", repeat_index=1, skipped=True),
        synthetic("a", repeat_index=2, skipped=True),
    ]
    item = probe_runner.aggregate_stability(records)["a"]
    assert item["attempts"] == 1
    assert item["skipped"] == 2
    assert item["passed"] == 1
    assert item["pass_rate"] == 1.0, "跳过不是失败，不该把通过率拽下去"
    assert item["metrics"]["steps"]["count"] == 1


def test_一次都没跑成的探针通过率是未知而不是零():
    item = probe_runner.aggregate_stability(
        [synthetic("a", skipped=True, check_ok=False, bounds_ok=False)]
    )["a"]
    assert item["attempts"] == 0
    assert item["pass_rate"] is None
    assert item["unstable"] is False


def test_聚合按探针分开且保持清单次序():
    records = [
        synthetic("a", check_ok=True),
        synthetic("b", check_ok=False, bounds_ok=False),
        synthetic("a", repeat_index=1, check_ok=False, bounds_ok=False),
    ]
    stability = probe_runner.aggregate_stability(records)
    assert list(stability) == ["a", "b"]
    assert stability["a"]["attempts"] == 2
    assert stability["a"]["pass_rate"] == 0.5
    assert stability["b"]["pass_rate"] == 0.0


def test_稳定度表把通过率与极差画得出来():
    records = [
        synthetic("merge", check_ok=True, bounds_ok=True, steps=3, wall_seconds=41.0),
        synthetic("merge", repeat_index=1, check_ok=False, bounds_ok=True, steps=9, wall_seconds=95.0),
    ]
    table = probe_runner.render_stability(records)
    assert "merge" in table
    assert "0.50" in table
    assert "3.0-9.0-6.0" in table
    assert "41.0-95.0-68.0" in table
    assert "不稳定" in table


def test_失败清单指向现场而不是只给一句话():
    record = synthetic("a", check_ok=False, bounds_ok=True, repeat_index=1)
    record["events_path"] = "/tmp/a-r1/.agent/probe.stdout.jsonl"
    item = probe_runner.aggregate_stability([record])["a"]
    failure = item["failures"][0]
    assert failure["repeat_index"] == 1
    assert failure["check_ok"] is False and failure["bounds_ok"] is True
    assert failure["events_path"] == "/tmp/a-r1/.agent/probe.stdout.jsonl"
    assert failure["temperature"] == 0.4 and failure["seed"] == 1
    text = probe_runner.render_failures([record])
    assert "/tmp/a-r1/.agent/probe.stdout.jsonl" in text


# --------------------------------------------------------------------------
# 温度 / 种子 / 第几次 落进记录
# --------------------------------------------------------------------------


def test_温度种子与第几次都落进记录(tmp_path):
    captured = []
    probe = probe_by_id("rename-cross-file")
    options = options_for(
        [
            "--probe",
            "rename-cross-file",
            "--temperature",
            "0.4",
            "--seed",
            "7",
            "--repeat",
            "3",
        ]
    )
    record = probe_runner.run_one(
        probe,
        MANIFEST,
        options,
        tmp_path,
        runner=fake_runner_factory(captured),
        python=sys.executable,
        repeat_index=2,
        repeat_total=3,
    )
    assert record["temperature"] == 0.4
    assert record["seed"] == 9, "第 2 次重复用基准 7 + 2"
    assert record["seed_base"] == 7
    assert record["repeat_index"] == 2
    assert record["repeat_total"] == 3
    for key in probe_runner.REQUIRED_RECORD_KEYS:
        assert key in record, f"记录缺字段 {key}"

    command = captured[0]["command"]
    assert command[command.index("--temperature") + 1] == "0.4"
    assert command[command.index("--seed") + 1] == "9", "下发的是这一次的种子"
    # 采样设置必须是**可复原**的：记录里读得出来，命令行也确实下发了
    assert record["command"] == command


def test_每次重复用不同的种子(tmp_path):
    """同一个种子 + 同一句提示词在温度 > 0 下还是同一条轨迹：

    那样「重复 N 次」测到的只是措辞差异，不是采样波动。所以按基准+序号取。
    """
    assert probe_runner.seed_for_attempt(None, 0) is None
    assert probe_runner.seed_for_attempt(None, 3) is None
    assert probe_runner.seed_for_attempt(1, 0) == 1
    assert probe_runner.seed_for_attempt(1, 1) == 2
    assert probe_runner.seed_for_attempt(2, 2) == 4

    captured = []
    options = options_for(["--probe", "fix-bug", "--seed", "1", "--repeat", "3"])
    probe = probe_by_id("fix-bug")
    seeds = []
    for index in range(3):
        record = probe_runner.run_one(
            probe,
            MANIFEST,
            options,
            tmp_path,
            runner=fake_runner_factory(captured),
            python=sys.executable,
            repeat_index=index,
            repeat_total=3,
        )
        seeds.append(record["seed"])
    assert seeds == [1, 2, 3]


def test_默认是温度零且不下发种子(tmp_path):
    """温度 0 时不给种子：历史读数一律在这种设置下取的。"""
    captured = []
    options = options_for(["--probe", "merge-html"])
    record = probe_runner.run_one(
        probe_by_id("merge-html"),
        MANIFEST,
        options,
        tmp_path,
        runner=fake_runner_factory(captured),
        python=sys.executable,
    )
    assert record["temperature"] == 0.0
    assert record["seed"] is None
    assert record["seed_base"] is None
    command = captured[0]["command"]
    assert command[command.index("--temperature") + 1] == "0.0"
    assert "--seed" not in command


def test_温度种子写进results_jsonl与summary的stability段(tmp_path, monkeypatch):
    monkeypatch.setattr(probe_runner, "RECORDS_DIR", tmp_path / "records")
    monkeypatch.setattr(probe_runner, "RESULTS_JSONL", tmp_path / "results.jsonl")
    records = [
        synthetic("merge", check_ok=True, bounds_ok=True, seed=2, temperature=0.7),
        synthetic(
            "merge",
            check_ok=True,
            bounds_ok=False,
            seed=2,
            temperature=0.7,
            repeat_index=1,
            variant_index=0,
            variant_source="goal_variants",
        ),
    ]
    for record in records:
        probe_runner.save_record(record, "20260101-000000")

    lines = [
        json.loads(line)
        for line in (tmp_path / "results.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [line["temperature"] for line in lines] == [0.7, 0.7]
    assert [line["seed"] for line in lines] == [2, 2]
    assert [line["variant_source"] for line in lines] == ["goal", "goal_variants"]
    assert [line["repeat_index"] for line in lines] == [0, 1]

    summary_path = tmp_path / "summary.json"
    probe_runner.save_summary(
        records,
        {"temperature": 0.7, "seed": 2, "repeat": 2},
        path=summary_path,
    )
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    assert payload["meta"]["temperature"] == 0.7
    assert "stability" in payload, "summary.json 必须有 stability 段"
    assert payload["stability"]["merge"]["pass_rate"] == 0.5
    assert payload["stability"]["merge"]["metrics"]["steps"]["span"] == 0.0


def test_重复时每次一个沙箱以免抹掉上一次的现场(tmp_path):
    assert probe_runner.sandbox_name("x", 0, 1) == "x"
    assert probe_runner.sandbox_name("x", 1, 2) == "x-r1"
    captured = []
    options = options_for(["--probe", "fix-bug", "--repeat", "2"])
    probe = probe_by_id("fix-bug")
    first = probe_runner.run_one(
        probe, MANIFEST, options, tmp_path, runner=fake_runner_factory(captured),
        python=sys.executable, repeat_index=0, repeat_total=2,
    )
    second = probe_runner.run_one(
        probe, MANIFEST, options, tmp_path, runner=fake_runner_factory(captured),
        python=sys.executable, repeat_index=1, repeat_total=2,
    )
    assert first["sandbox"] != second["sandbox"]
    assert Path(first["events_path"]).is_file()
    assert Path(second["events_path"]).is_file()


# --------------------------------------------------------------------------
# 变体只换说法
# --------------------------------------------------------------------------


def test_变体按次轮换且第一次永远是原话():
    probe = probe_by_id("merge-html")
    variants = probe["goal_variants"]
    first = probe_runner.goal_for_attempt(probe, 0)
    assert first["goal"] == probe["goal"]
    assert first["variant_index"] is None
    assert first["variant_source"] == "goal"

    rotated = [probe_runner.goal_for_attempt(probe, index) for index in (1, 2, 3)]
    assert [item["goal"] for item in rotated] == [variants[0], variants[1], variants[0]]
    assert [item["variant_index"] for item in rotated] == [0, 1, 0]
    assert all(item["variant_source"] == "goal_variants" for item in rotated)


def test_没给变体时退化为固定目标():
    probe = probe_by_id("merge-html")
    stripped = {key: value for key, value in probe.items() if key != "goal_variants"}
    for index in range(4):
        got = probe_runner.goal_for_attempt(stripped, index)
        assert got["goal"] == probe["goal"]
        assert got["variant_index"] is None
        assert got["variant_source"] == "goal"
    # 带变体的探针也能显式关掉轮换（对照组的写法）
    for index in range(4):
        got = probe_runner.goal_for_attempt(probe, index, rotate=False)
        assert got["goal"] == probe["goal"]
        assert got["variant_source"] == "goal"


def test_变体只换goal不动判据(tmp_path):
    captured = []
    probe = probe_by_id("merge-html")
    options = options_for(["--probe", "merge-html", "--repeat", "2"])
    record = probe_runner.run_one(
        probe,
        MANIFEST,
        options,
        tmp_path,
        runner=fake_runner_factory(captured),
        python=sys.executable,
        repeat_index=1,
        repeat_total=2,
    )
    command = captured[0]["command"]
    assert command[command.index("--goal") + 1] == probe["goal_variants"][0]
    # 判据那一侧一个字都没动：脚本还是清单里那一份，沙箱里的种子副本也没被改
    assert Path(record["check_script"]) == (
        PROBES_DIR / probe["check"]["script"]
    ).resolve(), "变体换的是 --goal，判据脚本还是清单里那一份"
    assert record["goal_base"] == probe["goal"]
    assert record["seed_unchanged"] is True
    assert record["guards_ok"] is True


def test_清单里每个探针都预注册了合法变体():
    """变体是这一层能力的原料：整批都没写，等于扰动只剩采样温度。"""
    for probe in MANIFEST["probes"]:
        variants = probe.get("goal_variants")
        assert variants, f"{probe['id']} 没写 goal_variants"
        assert len(variants) >= 2, f"{probe['id']} 的变体太少：{len(variants)}"
        assert probe["goal"] not in variants
        assert len(set(variants)) == len(variants), f"{probe['id']} 的变体互相重复"
        for variant in variants:
            assert isinstance(variant, str) and variant.strip()
            assert len(variant) >= 12, f"{probe['id']} 的变体太短：{variant}"


def test_不合法的变体要在清单校验时红(tmp_path):
    base = {
        "version": 1,
        "probes": [
            {
                "id": "x",
                "shape": "add-feature",
                "title": "t",
                "seed": "seeds/add-feature",
                "expected": "expected/add-feature",
                "goal": "把这件事做掉",
                "check": {"script": "checks/add_feature.py"},
            }
        ],
    }
    for bad in ([], [""], ["把这件事做掉"], ["没人会这么说"] * 2, [123]):
        payload = json.loads(json.dumps(base, ensure_ascii=False))
        payload["probes"][0]["goal_variants"] = bad
        path = tmp_path / "probes.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(probe_runner.ManifestError):
            probe_runner.load_manifest(path)

    payload = json.loads(json.dumps(base, ensure_ascii=False))
    payload["probes"][0]["goal_variants"] = ["换个说法把这件事做掉", "再做一遍这件事"]
    path = tmp_path / "ok.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    manifest = probe_runner.load_manifest(path)
    assert manifest["probes"][0]["goal_variants"][0] == "换个说法把这件事做掉"


# --------------------------------------------------------------------------
# 重复与有界
# --------------------------------------------------------------------------


def test_repeat会真的跑N次并逐次换说法(tmp_path):
    captured = []
    probe = probe_by_id("merge-html")
    code = probe_runner.main(
        [
            "--probe",
            "merge-html",
            "--root",
            str(tmp_path),
            "--no-save",
            "--temperature",
            "0.7",
            "--seed",
            "2",
            "--repeat",
            "3",
        ],
        runner=fake_runner_factory(captured),
    )
    assert code == 1, "假 runner 什么都没修好，判据必须是红的（红在下限过不过之外）"
    assert len(captured) == 3, "--repeat 3 就该跑 3 次"
    goals = [
        item["command"][item["command"].index("--goal") + 1] for item in captured
    ]
    assert goals == [probe["goal"], *probe["goal_variants"]]
    for index, item in enumerate(captured):
        assert item["command"][item["command"].index("--temperature") + 1] == "0.7"
        assert item["command"][item["command"].index("--seed") + 1] == str(2 + index)


def test_no_variants时每次都只用原话(tmp_path):
    captured = []
    probe = probe_by_id("merge-html")
    probe_runner.main(
        [
            "--probe",
            "merge-html",
            "--root",
            str(tmp_path),
            "--no-save",
            "--repeat",
            "3",
            "--no-variants",
        ],
        runner=fake_runner_factory(captured),
    )
    goals = [
        item["command"][item["command"].index("--goal") + 1] for item in captured
    ]
    assert goals == [probe["goal"]] * 3


def test_预算不够时先减少重复次数(tmp_path, capsys):
    """总预算按每一次检查：第一遍跑得下就跑，剩下的记为跳过并留下设置。

    假 runner 故意睡 3 秒：跑完第一遍，剩余预算必然掉到 60s 以下。
    """
    captured = []
    code = probe_runner.main(
        [
            "--probe",
            "merge-html",
            "--root",
            str(tmp_path),
            "--no-save",
            "--repeat",
            "3",
            "--temperature",
            "0.4",
            "--seed",
            "5",
            "--total-budget",
            "62",
        ],
        runner=fake_runner_factory(captured, sleep=3.0),
    )
    assert code == 1, "跑成的那一次判据是红的（假 runner 没修东西），跳过不算失败"
    assert len(captured) == 1, "预算只剩不到 60s，第二遍起就不再跑了"
    out = capsys.readouterr().out
    assert "第 2/3 次跳过" in out
    assert "0.4" in out
    # 跳过的那两次也带上了设置：报告里要说清「实际跑了多少、为什么」
    stability = probe_runner.aggregate_stability(
        [synthetic("merge-html", skipped=True, temperature=0.4, seed=5)]
    )["merge-html"]
    assert stability["skipped"] == 1 and stability["attempts"] == 0


def test_跳过记录带全设置字段():
    probe = probe_by_id("fix-bug")
    options = options_for(["--probe", "fix-bug", "--temperature", "0.4", "--seed", "5"])
    record = probe_runner.skipped_record(probe, options, 2, 3, 600, 580.0)
    assert record["skipped"] is True
    assert record["temperature"] == 0.4
    assert record["seed"] == 7, "第 2 次重复用基准 5 + 2"
    assert record["seed_base"] == 5
    assert record["repeat_index"] == 2 and record["repeat_total"] == 3
    assert record["variant_source"] == "goal_variants"
    for key in probe_runner.REQUIRED_RECORD_KEYS:
        assert key in record, f"跳过记录缺字段 {key}"
