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

带扰动的重复（测的是**波动**，不是复现性）：

    python scripts/probe.py --all --temperature 0 --repeat 1 --total-budget 1800
    python scripts/probe.py --all --temperature 0.4 --seed 1 --repeat 3 --total-budget 2400

为什么非要有这一层：温度 0 下连跑 3 次，三次事件流**字节完全相同**——那测到的
是「这份代码可复现」，不是「这份能力稳定」。要谈稳定，就得让采样真的抖起来
（`--temperature` + `--seed`），把同一句话说上几遍（`--repeat`），再看
**通过率**与**步数/token/墙钟的极差**。`--seed` 是基准：第 k 次重复用
`基准 + k`，这样每一次都是独立采样；而每一次用的种子都落在记录里，所以
单次仍然可复现（`seed` = 这一次实际下发的，`seed_base` = 命令行给的）。

三条不变式，写在最前面是因为它们是这个脚本存在的理由：

1. **绝不动种子目录、绝不动用户工作区。** 每个探针在 `--root` 下自己的沙箱副本里
   跑（`<root>/<id>`；重复多次时是 `<root>/<id>-r<第几次>`，这样每一次的现场
   ——事件流、判据输出——都留得住，失败轨迹才有得贴），跑完还要再算一遍种子
   指纹确认没被污染。
2. **工作区级授权必须真的生效。** 跑之前往沙箱写 `.agent/policy.json`
   （`{"policy":"auto"}`），命令行**不传** `--policy`——这样授权的生效路径就是
   真实的「读工作区策略」那条。写文件必须**无 BOM**：PowerShell 5.1 的
   `-Encoding utf8` 会带 BOM，而 `load_policy` 读不了 BOM 会静默退回 `ask`，
   于是每一步写操作都要确认，无人值守的探针直接跑挂。
3. **一次跑分有界。** 单探针超时、步数上限、整批总预算都可配；超时就强杀进程树
   并如实记录，绝不挂死。总预算按**每一次重复**检查：快撞预算时先减少重复次数，
   而不是把整条探针丢掉。

判据是**冻结**的：`checks/**`、`seeds/**`、`expected/**` 一个字节都不许动。
`goal_variants`（预注册措辞变体）只改「用户怎么说」，改不动判据——它轮换的是
`--goal`，判据脚本路径与 guard 哈希都还是清单里那一份。
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
    # 拆分后的四个量（护栏落地后新增）：总数里的「被拦未执行」与「未被拦」。
    # 必填是为了让老记录缺字段这件事**当场露出来**，而不是在下限判定里
    # 静默退化成「未被拦 = 总数」。
    "repeat_blocks_blocked",
    "repeat_blocks_unblocked",
    "read_blocks_blocked",
    "read_blocks_unblocked",
    "blocked_refunds",
    "combo_disabled",
    "supervisor_interventions",
    "supervisor_false_claims",
    "file_changed",
    "check_ok",
    "notes",
    # ---- 自动审查层 P0：只读数、不改结局的五个字段（设计稿 §6 字段表）--------
    #
    # 五个都从**事件流里的机械判据**读出来，不看模型的总结文字：
    #   `stale_references` / `tests_weakened` / `review_findings` / `evidence_gaps`
    # 是审查层发出来的 note 的条数；`false_completion` 由 `final_ok` 与
    # `check_ok` 派生（自认完成而验收红）。P0 **不许**因为多了这些字段而改变
    # `final_ok` / `check_ok` / 落盘——所以 `false_completion` 应与基线一致。
    "stale_references",
    "tests_weakened",
    "review_findings",
    "false_completion",
    "evidence_gaps",
    # ---- 自动审查层 P1：风险分级放行的读数（设计稿 §6 字段表）----------------
    #
    # 口径提醒（这三条都是**设计稿写在字段表里的**，实现不得走样）：
    #   `workspace_not_empty` **不是**「文件数不得减少」，也**不是**「不许删文件」
    #       ——它量的是「本轮报告产出过改动时，盘上能不能看到这些产出」；
    #       删除、合并、拆分让文件总数变少，只要产出在盘上就是 true。
    #   `changes_on_disk` 是「产出在盘上的路径数」，**删除也算一条**。
    #   `revert_available` 判的是「有没有回退」，**不是「有没有删东西」**。
    "workspace_not_empty",
    "changes_on_disk",
    "revert_available",
    "blocked_actions",
    "blocked_by_review",
    "blocked_but_ok",
    "escalations",
    "escalated_before_action",
    "escalation_summary_complete",
    "escalation_approved",
    # ---- 自动审查层 P2 后半：L3 语义审查的读数（设计稿 §6 字段表）------------
    #
    # 三个必填（判据口径写在字段表里，实现不得走样）：
    #   `review_l3_ran`    L3 **真的调用了模型**几次（触发条件不满足时是 0——
    #                      这是「不每步都跑」那条纪律的可证伪读数）。
    #   `l3_findings`      L3 报出的发现条数（**过没过事实核对都算**：被降级的
    #                      那几条同样进事件流与报告，只是不据此阻断）。
    #   `l3_blocked`       「L3 的高严重度发现让完成声明降级」判了几次。它
    #                      **不是**「成果没落盘」——那两件事在本设计里已分开。
    "review_l3_ran",
    "l3_findings",
    "l3_blocked",
    # ---- 带扰动的重复：这一次是「第几遍、用什么设置、说哪句话」 ----
    # 没有这四个字段，results.jsonl 就分不出「同一设置下的波动」和
    # 「不同设置之间的差异」，聚合只能靠猜。
    "temperature",
    "seed",
    "seed_base",
    "repeat_index",
    "repeat_total",
    "variant_index",
    "variant_source",
)

# 聚合时要看极差与均值的指标。只放「每次跑都必然报出来」的那几个：
# 步数、模型调用、输入/输出 token、墙钟。拦截计数不在这里——它们是判定下限的
# 证据，单次就能下结论，不需要极差。
STABILITY_METRICS = (
    "steps",
    "model_calls",
    "prompt_tokens",
    "completion_tokens",
    "wall_seconds",
)

# ---- 事件流里的字面量 -------------------------------------------------------
# 判定全靠这些字面量，所以集中放一处：改话术时这里跟着改，别散落在解析逻辑里。
BLOCKED_PREFIX = "⛔ 未执行："
REFUSAL_MARKER = "⚠ 拒绝整文件重写"
IDENTICAL_RETRY_MARK = "完全相同"
REPEAT_GUARD_MARK = "已经做过"
#: 「重复**真的执行了**」的两个开头（`loop.py` 只提醒、没有拦的那条路）。
#:
#: 为什么要它与上面那组分开：护栏自己就是拦截者之后，`⛔ 未执行` 意味着这一次
#: 调用**根本没有跑**——那是护栏在干活，不是空转。真空转是「同一个调用又跑了一
#: 遍、结果不会变」：护栏只提醒不拦的那种。两者混在一个数里，就会出现
#: 「任务判据全绿、分却是自己人扣的」。
REPEAT_EXECUTED_MARKS = (
    "注意：这一步与上一步完全相同",
    "注意：这个调用在最近几步里已经执行过",
)
VERIFIER_NAMES = ("自动验证", "最终验证")
READ_TOOLS = frozenset(
    {"read_file", "list_dir", "search_code", "dir_stats", "read_diagnosis"}
)
SUPERVISOR_PREFIX = "督导："
GUARD_REFRESH_KIND = "guard_refresh"
#: 「被拦下、没有执行」的一步退还了步数预算（`loop.py` 的 `blocked_refund`）。
#: 它是可覆盖上限内的退还，报告里要能看见——否则「这次为什么没吃预算」只能猜。
BLOCKED_REFUND_KIND = "blocked_refund"
#: 同一个（工具 + 参数）被拦到阈值之后停用（`loop.py` 的 `combo_disabled`）。
#: 它量的是「拦截换路了没有」，与上面的拦截计数分开数。
COMBO_DISABLED_KIND = "combo_disabled"
FACT_CONFLICT_KIND = "supervisor_fact_conflict"

# ---- 自动审查层 P0：事件流里的审查 note kind ---------------------------------
#
# 判据名在 `spoolkit.agent.audit` / `spoolkit.tools.edit` 里定义，这里只按
# kind 计数（**不读散文**）。`stale_references` 收的是残留引用与「删掉的文件
# 还有人引用」两类；`broken_import` 是「导入的本地模块不存在」。
AUDIT_KIND = "audit"
STALE_REFERENCES_KIND = "stale_references"
BROKEN_IMPORT_KIND = "broken_import"
TESTS_WEAKENED_KIND = "tests_weakened"
EVIDENCE_GAPS_KIND = "evidence_gaps"
# 风险分级放行（P1，§3.2）：`escalate` 是主动请示，`action_block` 是危险动作
# 被拦下/被评估，`blocked_by_review` 是「不许宣称完成」那一次判定。
ESCALATE_KIND = "escalate"
ACTION_BLOCK_KIND = "action_block"
BLOCKED_REVIEW_KIND = "blocked_by_review"
#: 「拦它的理由是 L1/L2 真的报了高严重度发现」。与上面那个分开数，是为了让
#: `blocked_but_ok`（误报代价）只落在**机械判据报了真缺陷**的那一类上：
#: 「验收判据看不见、而机械判据看见了」是审查层的**收益**，不是误报。
BLOCKED_REVIEW_HARD_KIND = "blocked_by_review_finding"
#: 算「审查发现条数」的那几类 note（`evidence_gaps` 是账本读数，不算发现；
#: 升级与阻断各自有专门的字段，也不混进发现条数）。
#:
#: L2 的三类（P2）也在这里：行为回归 / 命令行接口变化 / 性能倒退都是**发现**，
#: 探针要按 kind 分开数（`review_findings_by_kind`），不能混进 `audit` 靠读散文认。
BEHAVIOR_CHANGED_KIND = "behavior_changed"
CLI_SURFACE_CHANGED_KIND = "cli_surface_changed"
PERFORMANCE_REGRESSED_KIND = "performance_regressed"
#: L2 的「查不了」读数（不是发现，不进 `review_findings`）：性能那一条在
#: 耗时太短或落在噪声带里时**如实报「量不出来」**，不静默通过。
BEHAVIOR_NOT_CHECKED_KIND = "behavior_not_checked"
#: L3 语义审查（P2 后半）的发现（**是发现**：混进 `review_findings` 的总额，
#: 同时单独数 `l3_findings`）。判据名与 `spoolkit/agent/audit.py` 逐字一致。
SEMANTIC_FINDING_KIND = "semantic_finding"
#: L3 的过程读数（触发条件 / 跑没跑 / 被事实核对剔除几条 / 为什么没给结论）。
#: **不是发现**，与 `BEHAVIOR_NOT_CHECKED_KIND` 同一个口径。
L3_READINGS_KIND = "l3_readings"
#: L3 发现的**结构化**那一份（每条带 file/line/severity/verified）。它也不是
#: 发现——发现那一条已经在上面的 `SEMANTIC_FINDING_KIND` 里数过一次了。
L3_DETAIL_KIND = "l3_detail"
#: 「L3 拦下了完成声明」（探针的 `l3_blocked`）。
L3_BLOCKED_KIND = "l3_blocked"
REVIEW_NOTE_KINDS = frozenset(
    {
        AUDIT_KIND,
        STALE_REFERENCES_KIND,
        BROKEN_IMPORT_KIND,
        TESTS_WEAKENED_KIND,
        BEHAVIOR_CHANGED_KIND,
        CLI_SURFACE_CHANGED_KIND,
        PERFORMANCE_REGRESSED_KIND,
        SEMANTIC_FINDING_KIND,
    }
)

#: 落盘读数时**不算产出**的前缀：这些是 harness 自己写的东西（授权、检查点、
#: 索引、记忆库、事件流本身），不是模型的产出。把它们算成 `changes_on_disk`
#: 会让「成果在不在盘上」永远为真，那个字段就废了。
_HARNESS_PREFIXES = (".agent/",)

#: 工作区根下不需要工具就能直接跑一遍的回退命令形态（`spool revert --run <id>`）。
REVERT_COMMAND_PREFIX = "spool revert --run"

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
        _validate_variants(where, probe)
    return {
        "version": payload.get("version", 1),
        "defaults": defaults,
        "probes": probes,
        # 清单自己的口径说明（`_limits_note`）：下限为什么这样定，写在清单里
        # 而不是只写在代码注释里——读清单的人看不到 `probe.py` 的注释。
        "limits_note": list(payload.get("_limits_note") or []),
    }


def _validate_variants(where, probe):
    """`goal_variants` 是可选的预注册措辞变体，校验得比 goal 更严。

    为什么严：变体是**同一件事的另一种说法**，所以它必须与原话、与彼此都不
    相同——一模一样的「变体」等于没变，而它会在聚合里被当成「换过说法」。
    ⚠️ 变体只影响 `--goal`，绝不影响判据：`checks/**`、`seeds/**`、
    `expected/**` 都不参与这里的任何判断。
    """
    variants = probe.get("goal_variants")
    if variants is None:
        return
    if not isinstance(variants, list) or not variants:
        raise ManifestError(
            f"{where}.goal_variants 必须是非空数组；不用它就把这一项整个去掉"
        )
    seen = {probe["goal"]}
    for index, variant in enumerate(variants):
        if not isinstance(variant, str) or not variant.strip():
            raise ManifestError(f"{where}.goal_variants[{index}] 必须是非空字符串")
        if variant in seen:
            raise ManifestError(
                f"{where}.goal_variants[{index}] 与目标原话或别的变体重复："
                f"{variant[:40]}"
            )
        seen.add(variant)


def find_probe(manifest, identifier):
    for probe in manifest["probes"]:
        if probe["id"] == identifier:
            return probe
    raise ManifestError(f"清单里没有这个探针：{identifier}")


def seed_for_attempt(seed, repeat_index):
    """这一次重复实际用的种子。`--seed` 是**基准**，第 k 次用 `基准 + k`。

    为什么不是每次都同一个种子：llama.cpp 的种子是**每次请求**重新初始化的，
    同一个种子配同一句提示词，在温度 > 0 下采样出来的还是同一条轨迹——那样
    「重复 3 次」测到的只是措辞差异，不是采样波动。基准加序号让每一次都是
    独立采样，而每一次用的种子都落进记录，所以单次仍然可复现。
    """
    if seed is None:
        return None
    return int(seed) + int(repeat_index)


def goal_for_attempt(probe, repeat_index, rotate=True):
    """这一次重复该用哪句「用户原话」。

    - 第 0 次永远是清单里的原话：基线读数要跟历史记录对得上；
    - 之后按 `goal_variants` 轮换（写几个变体、重复几次就转几格，绕回开头）；
    - **没写变体就退化成固定目标**，`--no-variants` 也走这一支。

    变体只换说法，不换判据：这里返回的字符串唯一去向是 `--goal`。
    """
    base = probe["goal"]
    variants = list(probe.get("goal_variants") or [])
    if not variants or not rotate or repeat_index <= 0:
        return {
            "goal": base,
            "variant_index": None,
            "variant_source": "goal",
        }
    index = (repeat_index - 1) % len(variants)
    return {
        "goal": variants[index],
        "variant_index": index,
        "variant_source": "goal_variants",
    }


# --------------------------------------------------------------------------
# 沙箱与授权
# --------------------------------------------------------------------------


def default_root():
    """不给 --root 时的沙箱根：系统临时目录，一次跑分一个时间戳。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return Path(tempfile.gettempdir()) / "spoolkit-probes" / stamp


def sandbox_name(identifier, repeat_index=0, repeat_total=1):
    """这一次重复的沙箱目录名。

    重复多次时每次一个（`-r0`/`-r1`/…）：`run_one` 会把沙箱整个重建，共用一个
    目录就等于「上一条的现场被下一条抹掉」——而失败轨迹正是从现场里读出来的。
    """
    if repeat_total <= 1:
        return identifier
    return f"{identifier}-r{repeat_index}"


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


def _envcheck():
    """取 `spoolkit.envcheck`（探针平时只跑子进程，从不 import 仓库代码）。

    候选目录与"补 PATH"这件事现在只有一份实现：真机跑分的那个脚本以前自己
    另建了一套子进程环境、没走这条路，于是 A 类格子在没 rg 的环境里跑出了
    假的「可用性 0/3」——补上 rg 之后同一批格子变成 3/3。别名不是省事，是
    为了不再长出第二份。
    """
    src = str(REPO_ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from spoolkit import envcheck

    return envcheck


def _tool_dirs():
    """`rg` 与 `node` 的候选目录（唯一实现在 `spoolkit.envcheck.probe_tool_dirs`）。

    `tools/search.py` 依赖 rg；HTML 的内联 JS 验证依赖 node。PATH 里没有它们时，
    「找不到 rg」会被记成工具失败、「找不到 node」会被 HTML 验证器报成
    「查不了」——两条都不是模型的问题，却会把探针读数染脏。
    """
    return _envcheck().probe_tool_dirs()


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
    # 跑 llama.cpp 的本机服务，别让系统代理把回环流量劫走：环境自检只警告、
    # 不改环境，而子进程 httpx 虽然 trust_env=False，别的读环境路径仍会走代理。
    _envcheck().isolate_local_http(env)
    for stale in ("AGENTS_DEV_CONFIG", "AGENTS_DEV_ENV_FILE", "AGENTS_DEV_STATE"):
        env.pop(stale, None)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "src"), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    added = _envcheck().ensure_probe_tools(env, ("rg", "node"))
    env["_PROBE_PATH_ADDED"] = ";".join(added)
    return env


def env_self_check(*, window=0, stream=None):
    """开跑之前的测量环境自证：不合格就别开跑。

    跑完再解释是次优的——模型那一轮的 token 已经花掉了，而且读数还会被当成
    「模型不行」。所以自证放在**第一批之前**，退出码沿用
    `bench.EXIT_ENVIRONMENT_BLOCKED`（和「pytest 不在就别开跑」同一个口径）。

    量的是**子进程将看到的那一份环境**：候选目录补 PATH 走的就是子进程那条路，
    所以拿同一份 `ensure_probe_tools` 结果去判，而不是判探针自己所在的 PATH。
    """
    envcheck = _envcheck()
    probe_ready = dict(os.environ)
    envcheck.ensure_probe_tools(probe_ready, ("rg", "node"))
    report = envcheck.inspect(env=probe_ready, window=window or None)
    code = envcheck.require_or_exit(
        report, stream=sys.stderr if stream is None else stream
    )
    return code, report


def build_command(probe, sandbox, options):
    """拼出这次要跑的 spool 命令。

    **不传 `--policy`**：授权的生效路径要走工作区里的 `.agent/policy.json`，
    那正是这条不变式要验的东西。`--scope` 是另一件事（自动落盘的范围），
    它没有工作区级存储，只能由命令行给。

    `--goal` 取 `options["goal"]`（这一次重复实际要说的那句话），温度与种子
    显式下传：读数必须能回答「这是在什么采样设置下取的」。
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
        options.get("goal") or probe["goal"],
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
    # 温度总是显式给：不给的话「这一次用的是默认 0」就只存在于读代码的人的脑子里，
    # 而不在记录里。种子只在给了的时候给——温度 0 下它不起作用。
    if options.get("temperature") is not None:
        command += ["--temperature", str(options["temperature"])]
    if options.get("seed") is not None:
        command += ["--seed", str(options["seed"])]
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


def _repeat_executed(detail):
    """这一次「同一个调用又发了一遍」是不是**真的执行了**（护栏只提醒没拦）。"""
    return any(mark in detail for mark in REPEAT_EXECUTED_MARKS)


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
        # 「被硬拦下、**没有执行**」的那一部分。`repeat_blocks` 是**总数**
        # （被拦的 + 护栏只提醒没拦的），这两个数分开之后，
        # 「我们扣的分（被拦）」与「真病态（未被拦的重复）」才分得开。
        "repeat_blocks_blocked": 0,
        "read_blocks": 0,
        "read_blocks_blocked": 0,
        "rewrite_refusals": 0,
        "guard_refreshes": 0,
        # 「被拦下、没有执行」的一步退还了几步（`blocked_refund` 事件里的次数）。
        # 它是**可见性**读数：退还有上限，报告与事件流里都要能看出「这次因被拦
        # 而退还」。它不是「拦截次数」——拦截次数看上面的 `blocked_total`。
        "blocked_refunds": 0,
        # 同一个（工具 + 参数）被拦到阈值之后**停用**了几次（`combo_disabled`）。
        # 这一条量的是「拦截有没有真的换路」，而不是「拦了几次」。
        "combo_disabled": 0,
        "supervisor_interventions": 0,
        "supervisor_false_claims": 0,
        # 自动审查层 P0 的读数（见 §6 字段表）。全部按事件流的 note kind 数，
        # 不看模型的总结文字。
        "stale_references": 0,
        "broken_imports": 0,
        "tests_weakened": 0,
        "review_findings": 0,
        "review_findings_by_kind": {},
        "evidence_gaps": 0,
        # ---- 风险分级放行（P1）----------------------------------------------
        "escalations": 0,
        "escalations_complete": 0,
        "escalated_before_action": 0,
        "escalation_triggers": {},
        # 这一组（= 这一次收尾判定）里的触发条件。**一次打扰只算一次**，
        # 见 `parse_events` 里 `escalation_groups` 那段。
        "escalation_groups": {},
        "escalation_group": "（开场）",
        "escalation_pending_group": "",
        "escalation_complete_ok": {},
        "escalation_before_ok": {},
        "escalation_approved": None,
        "blocked_actions": 0,
        "actions_gated": 0,
        "blocked_by_review": 0,
        "blocked_by_finding": 0,
        "review_triggers": [],
        "revert_commands": [],
        # ---- L3 语义审查（P2 后半，§6 字段表）--------------------------------
        #
        # `review_l3_ran` 数的是**真的调用了模型**那几次：L3 的读数事件里
        # `ran=True` 才算（触发条件不满足时也发一条读数，如实写 0——那是
        # 「不每步都跑」的读数，不是一次审查）。
        "review_l3_ran": 0,
        "l3_findings": 0,
        "l3_unverified": 0,
        "l3_blocked": 0,
        "l3_findings_by_severity": {},
        "l3_conflicts": {},
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
        # ---- P0 指标：**有没有写出结论**、**谁终止的** ------------------------
        #
        # 这两个字段是两条「能证伪」的读数，口径**只有一个**出口
        # （`loop.py` 的 `LoopResult.concluded` / `.terminated_by`，
        # 经 FINAL 事件下发）——不在跑分器里靠正则去猜。
        #
        # 为什么要它们：真机对照 36 次里 33 次没有结论，而旧读数只有
        # `final_ok`（「模型自认完成」）一个布尔——「模型写了结论但被审查拦下」
        # 与「模型一个字都没写、只有系统判词」在那个布尔里长得一模一样。
        "concluded": False,
        #: 模型自己写的结论原文（判词与账目不在里面）——可用性判据读它，
        #: 不再读那段混着系统判词的报告正文。
        "model_final": "",
        #: 谁终止：模型完成 / 步数上限 / 超时 / 失败。超时由跑分器按墙钟填。
        "terminated_by": "",
        #: 失败的原话（空串 = 没失败）。`terminated_by == "失败"` 时非空。
        "failed_reason": "",
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
                # 「没有校验器」那一档**什么都没验**（问题⑤）：它既不进
                # `verifier_ran`，也不算「通过」——把「没验」数成「验过且通过」
                # 会让读数说一件不成立的事（`no_checker` 由循环发出来）。
                if payload.get("no_checker"):
                    continue
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
                    counters["repeat_blocks_blocked"] += 1
                if name in READ_TOOLS:
                    counters["read_blocks"] += 1
                    counters["read_blocks_blocked"] += 1
            elif _repeat_executed(detail):
                # 护栏只提醒、没有拦：**这一次真的又跑了一遍**，是真空转。
                # 它计入总数、不计入 blocked——下限就是拿它判的。
                counters["repeat_blocks"] += 1
                if name in READ_TOOLS:
                    counters["read_blocks"] += 1
        elif kind == "note":
            note_kind = str(payload.get("kind") or "")
            note_text = str(payload.get("text") or "")
            if note_kind == GUARD_REFRESH_KIND:
                counters["guard_refreshes"] += 1
            if note_kind == BLOCKED_REFUND_KIND:
                # 取最大值而不是累加：事件里带的是「已退还 n/上限」，一条 note
                # 就是一次退还，但重复发出同一条不该把它数成两次。
                counters["blocked_refunds"] = max(
                    counters["blocked_refunds"], int(payload.get("count") or 1)
                )
            if note_kind == COMBO_DISABLED_KIND:
                counters["combo_disabled"] += 1
            if note_kind == FACT_CONFLICT_KIND:
                counters["supervisor_false_claims"] += 1
            if note_kind in REVIEW_NOTE_KINDS:
                # 一条 note 就是一条发现（审查层按 (判据, 文件, 行, 摘要) 去过重，
                # 所以同一条不会在多个验证点被重复计一遍）。
                counters["review_findings"] += 1
                by_kind = counters["review_findings_by_kind"]
                by_kind[note_kind] = by_kind.get(note_kind, 0) + 1
                if note_kind == STALE_REFERENCES_KIND:
                    counters["stale_references"] += 1
                elif note_kind == TESTS_WEAKENED_KIND:
                    counters["tests_weakened"] += 1
                elif note_kind == BROKEN_IMPORT_KIND:
                    counters["broken_imports"] += 1
            if note_kind == EVIDENCE_GAPS_KIND:
                # 账本读数：一次运行里「没有证据的完成声明」条数（取最大值——
                # 重复发同一句话不该把它累加成一堆）。
                counters["evidence_gaps"] = max(
                    counters["evidence_gaps"], int(payload.get("count") or 1)
                )
            # ---- 风险分级放行（P1，§6 字段表）--------------------------------
            #
            # 三个 kind 各回答一件事，**不要把它们混成一个数**：
            #   `escalate`        主动请示了一次（拦的是动作 / 结论）
            #   `action_block`    危险动作被带进闸门；`gated=True` 是「该拦就拦」
            #                     的读数，`blocked`/无 `gated` 的是真被拦下的
            #   `blocked_by_review` 「不许宣称完成」判了一次（**不是**成果没落盘）
            if note_kind == ESCALATE_KIND:
                # **一次打扰只算一次**（§3.2.4 的 `escalation_rate` 量的是
                # 「每轮打断了用户几次」）：同一轮里 T1/T7/T12 同时命中，请示
                # 只发生一次（五件里逐条列了三个条件），逐条累加会把一次打扰
                # 读成三次。所以按「**这一次收尾判定**」归组：同一组只算一次。
                trigger = str(payload.get("trigger") or "") or "（未点名）"
                by_trigger = counters["escalation_triggers"]
                by_trigger[trigger] = by_trigger.get(trigger, 0) + 1
                # 这一组的名字：优先挂在**当前这一次 blocked_by_review** 上
                # （同一轮的三条 escalate 事件共享它）；还没见到它就先按触发
                # 条件各自成组，并把组名记进 `escalation_pending_group`——
                # 见到收尾判定那条时会被并进来（见下面 `blocked_by_review` 那段）。
                if counters["escalation_group"] == "（开场）":
                    counters["escalation_group"] = f"escalate#{trigger}"
                    counters["escalation_pending_group"] = counters["escalation_group"]
                counters["escalation_groups"].setdefault(
                    counters["escalation_group"], []
                ).append(trigger)
                # 五件齐不齐、拦在动作前没有：**按组记**（一次打扰是一整份请示，
                # 组内任一事件缺件就算这次请示不合格）。
                group = counters["escalation_group"]
                counters["escalation_complete_ok"][group] = bool(
                    payload.get("summary_complete")
                ) and counters["escalation_complete_ok"].get(group, True)
                counters["escalation_before_ok"][group] = bool(
                    payload.get("before_action")
                ) and counters["escalation_before_ok"].get(group, True)
                # 五件里的第 ⑤ 件（回退命令）落在这条 note 的正文里：
                # `revert_available` 据此判定「给不给得出一条可执行的整轮回退」。
                for line in note_text.splitlines():
                    if REVERT_COMMAND_PREFIX in line:
                        counters["revert_commands"].append(line.strip())
            elif note_kind == ACTION_BLOCK_KIND:
                if payload.get("gated"):
                    counters["actions_gated"] += 1
                elif payload.get("trigger"):
                    counters["blocked_actions"] += 1
            elif note_kind in (BLOCKED_REVIEW_KIND, BLOCKED_REVIEW_HARD_KIND):
                counters["blocked_by_review"] += 1
                if note_kind == BLOCKED_REVIEW_HARD_KIND:
                    counters["blocked_by_finding"] += 1
                # 这一次收尾判定 = 一次打扰：**上一批还没归组的 escalate 事件
                # 并到这一组里**（同一轮里 T1/T7/T12 三条 escalate 事件先发、
                # 收尾判定那条后发，所以在这里收口）。
                counters["escalation_group"] = (
                    f"{note_kind}#{counters['blocked_by_review']}"
                )
                loan = counters.pop("escalation_pending_group", None)
                if loan and loan in counters["escalation_groups"]:
                    counters["escalation_groups"][
                        counters["escalation_group"]
                    ] = counters["escalation_groups"].pop(loan)
                for trigger in payload.get("triggers") or []:
                    if trigger not in counters["review_triggers"]:
                        counters["review_triggers"].append(str(trigger))
            elif note_kind == "revert_available":
                # 这一轮给得出一条可执行的整轮回退命令（§6 字段表：
                # `revert_available` 判的是「有没有回退」，不是「有没有删东西」）。
                command = str(payload.get("revert_command") or "")
                if command and command not in counters["revert_commands"]:
                    counters["revert_commands"].append(command)
            elif note_kind == "escalation_reply":
                approved = payload.get("approved")
                if approved is None:
                    continue
                # 同一条运行里多次批复时，只要有一次挂起待批就报 `pending`
                # （「有动作没执行」这件事比「另一个动作被批了」更该被看见）。
                if counters["escalation_approved"] == "pending":
                    continue
                counters["escalation_approved"] = approved
            # ---- L3 语义审查（P2 后半，§6 字段表）----------------------------
            #
            # 四条 kind 各回答一件事，**不要混成一个数**：
            #   `l3_readings`    跑没跑（`ran`）+ 触发条件 + 被剔除几条 + 冲突分档
            #   `l3_detail`      逐条发现的结构化字段（file/line/severity/verified）
            #   `semantic_finding` 一条发现（上面 `REVIEW_NOTE_KINDS` 已经数过，
            #                    这里只补 `l3_findings` 这个**分层**读数）
            #   `l3_blocked`     「L3 的高严重度发现让完成声明降级」
            if note_kind == L3_READINGS_KIND:
                # `ran` 为真才算一次真审查：触发条件不满足时也发读数（如实写 0），
                # 那是「不每步都跑」的读数，不是一次模型调用。
                if payload.get("ran"):
                    counters["review_l3_ran"] += 1
                # 被核对剔除 / 降级的条数取最大值：同一轮重算一次不该翻倍。
                counters["l3_unverified"] = max(
                    counters["l3_unverified"], int(payload.get("unverified") or 0)
                )
                for key, value in (payload.get("conflicts") or {}).items():
                    bucket = counters["l3_conflicts"]
                    bucket[str(key)] = max(int(bucket.get(str(key), 0)), int(value or 0))
            elif note_kind == L3_DETAIL_KIND:
                for item in payload.get("findings") or []:
                    if not isinstance(item, dict):
                        continue
                    severity = str(item.get("severity") or "")
                    bucket = counters["l3_findings_by_severity"]
                    bucket[severity] = bucket.get(severity, 0) + 1
            elif note_kind == SEMANTIC_FINDING_KIND:
                counters["l3_findings"] += 1
            elif note_kind == L3_BLOCKED_KIND:
                # 与 `blocked_by_review` 一样取「判了几次」的**运行**口径：
                # 同一个运行里重算多次不该累加，所以按存在性取 1（事件本身
                # 由循环保证一次运行只发一条，这里再兜一层）。
                counters["l3_blocked"] = max(
                    counters["l3_blocked"], 1
                )
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
            # P0：结论与终止者都是 FINAL 上的字段。**旧事件流里没有它们**
            # （老记录、假 runner 的夹具）——那就不猜：`concluded` 退回
            # 「模型自认完成」，`terminated_by` 留空由收尾处按已知事实补。
            if "concluded" in payload:
                counters["concluded"] = bool(payload.get("concluded"))
            else:
                counters["concluded"] = counters["final_ok"]
            counters["model_final"] = str(payload.get("model_final") or "")
            counters["terminated_by"] = str(payload.get("terminated_by") or "")
            counters["failed_reason"] = str(payload.get("failed_reason") or "")
            counters["settled"] = str(payload.get("settled") or "")
            # 「有退路」这件事现在的**结构化**读数是 FINAL 上的字段（收尾报告
            # 里那条撤销提示落在 FINAL 正文的最后一行，所以它自然排在
            # 「等你确认 / 已应用」之后——问题①）。旧的那条 `revert_available`
            # note 已不再发：同一件事说两遍正是问题②。
            command = str(payload.get("revert_command") or "")
            if command and command not in counters["revert_commands"]:
                counters["revert_commands"].append(command)

    counters["verifier_ok"] = verdicts[-1] if verdicts else None
    # **打扰次数** = 有几组（= 做了几次收尾判定 / 问了几次）：同一轮里
    # T1/T7/T12 同时命中只算一次——请示只发生一次，五件里逐条列了三个条件。
    # 逐条累加会把一次打扰读成三次，升级率凭空翻三倍（§3.2.4 量的是
    # 「每轮打断了用户几次」）。`escalation_by_trigger` 仍是**按条件**分档，
    # 那是「哪个条件在刷屏」要的读数。
    counters["escalations"] = len(
        [group for group in counters["escalation_groups"].values() if group]
    )
    # 「一次打扰一份请示」：两个布尔按**组**汇总（缺件 / 拦晚了的那一组算不合格）。
    counters["escalations_complete"] = len(
        [ok for ok in counters["escalation_complete_ok"].values() if ok]
    )
    counters["escalated_before_action"] = len(
        [ok for ok in counters["escalation_before_ok"].values() if ok]
    )
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
    temperature = (
        args.temperature
        if getattr(args, "temperature", None) is not None
        else defaults.get("temperature")
    )
    seed = args.seed if getattr(args, "seed", None) is not None else defaults.get("seed")
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
        # 采样设置：不给温度就是 0（与历史读数一致），不给种子就让服务端自己取。
        "temperature": 0.0 if temperature is None else float(temperature),
        "seed": None if seed is None else int(seed),
        # 变体轮换默认开；`--no-variants` 用来做「固定目标」的对照。
        "rotate_variants": not bool(getattr(args, "no_variants", False)),
    }


def skipped_record(probe, options, repeat_index, repeat_total, budget, elapsed):
    """预算不够时留下的那条记录。

    它**不是一次尝试**（聚合里既不算分子也不算分母），但仍然带全温度/种子/
    第几次：不然「这个设置下少采了几次」这件事在记录里就查不到了——
    而报告里必须说得清「实际跑了多少、为什么」。
    """
    variant = goal_for_attempt(
        probe, repeat_index, rotate=options.get("rotate_variants", True)
    )
    return {
        "id": probe["id"],
        "shape": probe["shape"],
        "title": probe.get("title", ""),
        "goal": variant["goal"],
        "goal_base": probe["goal"],
        "variant_index": variant["variant_index"],
        "variant_source": variant["variant_source"],
        "temperature": options["temperature"],
        "seed": seed_for_attempt(options.get("seed"), repeat_index),
        "seed_base": options.get("seed"),
        "repeat_index": repeat_index,
        "repeat_total": repeat_total,
        "skipped": True,
        "notes": f"整批预算 {budget}s 不够了（已用 {elapsed:.0f}s），这一次没跑",
        "wall_seconds": 0.0,
        "final_ok": False,
        "check_ok": False,
        "bounds_ok": False,
        # P0 指标在跳过记录里也要有（字段齐了，聚合与对照才能按同一个口径取）：
        # 跳过的那一次没跑，所以「有没有写结论」是「没有」，「谁终止的」是
        # 「跳过」——**不是**「失败」，也不是「步数上限」。两者的差别要留着：
        # 前者是我们自己的预算不够，后者是模型没做完，混起来就没人分得清
        # 「这批跑了几次」与「这几次里有几次不收敛」。
        "concluded": False,
        "model_final": "",
        "terminated_by": "跳过",
        "failed_reason": "",
        "steps": None,
        "model_calls": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "verifier_ran": 0,
        "verifier_ok": None,
        "repeat_blocks": 0,
        "read_blocks": 0,
        # 拆分的两个量：跳过的那一次什么都没跑，四个读数都是 0（字段仍要齐）。
        "repeat_blocks_blocked": 0,
        "repeat_blocks_unblocked": 0,
        "read_blocks_blocked": 0,
        "read_blocks_unblocked": 0,
        # 两条可见性读数：跳过的那一次没跑，所以都是 0（字段仍要齐）。
        "blocked_refunds": 0,
        "combo_disabled": 0,
        "supervisor_interventions": 0,
        "supervisor_false_claims": 0,
        # 跳过的那一次什么都没跑，读数是 0（字段仍要齐：聚合与对照都按它取）。
        "stale_references": 0,
        "tests_weakened": 0,
        "review_findings": 0,
        "false_completion": False,
        "evidence_gaps": 0,
        # P1：跳过的那一次什么都没跑，读数是 0（字段仍要齐：聚合与对照都按它取）。
        # `workspace_not_empty` 为真：没产出过改动时它**不该**报红——这条判据量的是
        # 「有产出就必须看得见」，不是「文件数不得减少」。
        "workspace_not_empty": True,
        "changes_on_disk": 0,
        "revert_available": True,
        "blocked_actions": 0,
        "blocked_by_review": 0,
        "blocked_but_ok": False,
        "escalations": 0,
        "escalated_before_action": 0,
        "escalation_summary_complete": True,
        "escalation_approved": None,
        # ---- L3 语义审查（P2 后半，§6 字段表）--------------------------------
        #
        # 这次有没有给出一轮可执行的整轮回退命令（§2.5 第 2 条）。判的是
        # 「有没有回退」，**不是**「有没有删东西」。
        "revert_available": True,
        # 跳过的那一次没跑任何审查：三个读数都是 0（字段仍要齐：聚合与对照
        # 都按它取）。`l3_findings_by_severity` / `l3_conflicts` 是分档读数。
        "review_l3_ran": 0,
        "l3_findings": 0,
        "l3_unverified": 0,
        "l3_blocked": 0,
        "l3_findings_by_severity": {},
        "l3_conflicts": {},
        "file_changed": {"changed": False},
        "exit_code": None,
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def _tally_terminated(records) -> dict:
    """终止者的分档计数（含跳过那一档）。报告里按它念分布。"""
    tally: dict[str, int] = {}
    for item in records:
        key = str(item.get("terminated_by") or "—")
        tally[key] = tally.get(key, 0) + 1
    return dict(sorted(tally.items(), key=lambda kv: (-kv[1], kv[0])))


def derive_terminated_by(counters, outcome) -> str:
    """谁终止了这一次：模型完成 / 超时 / 失败 / 步数上限。

    **顺序就是判据的优先级**，而且每一条都对应一个机械事实：
    ① 进程超时被强杀（跑分器自己看的墙钟）；
    ② 进程没能启动；
    ③ 主循环报出来的 `terminated_by`（它知道自己是怎么结束的）；
    ④ 事件流里的失败标记 / `failed_reason`；
    ⑤ 进程异常退出（退出码既不是 0 也不是 1 —— 那两种是 `spool run` 的
       「完成 / 未完成」）且事件流里连一条 final 都没有：那是进程带着回溯死了，
       与「任务没做完」不是一回事；
    ⑥ 模型自己写了结论；
    ⑦ 其余（撞上步数上限、空回合中断、被拦下收尾）。
    """
    if outcome.get("timed_out"):
        return "超时"
    if outcome.get("spawn_error"):
        return "失败"
    reported = str(counters.get("terminated_by") or "")
    if reported:
        return reported
    if counters.get("failed_reason"):
        return "失败"
    code = outcome.get("exit_code")
    if not counters.get("final_text") and code not in (0, 1, None):
        return "失败"
    if counters.get("concluded"):
        return "模型完成"
    return "步数上限"


def run_one(
    probe,
    manifest,
    options,
    root,
    runner=run_spool,
    python=None,
    repeat_index=0,
    repeat_total=1,
    variant=None,
):
    """跑一个探针的一次重复，返回一条记录。这是整个脚本的主干。"""
    python = Path(python or repo_python())
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    seed = (PROBES_DIR / probe["seed"]).resolve()
    sandbox = root / sandbox_name(probe["id"], repeat_index, repeat_total)
    # 这一次的采样设置：种子按基准+序号取，记录里两个都留（基准=命令行给的，
    # 实际=这一次真正下发的），这样「哪一次是同一个设置下的独立采样」查得出来。
    seed_base = options.get("seed")
    attempt_seed = seed_for_attempt(seed_base, repeat_index)
    if variant is None:
        variant = goal_for_attempt(
            probe, repeat_index, rotate=options.get("rotate_variants", True)
        )
    options = dict(options, goal=variant["goal"], seed=attempt_seed)

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
    terminated_by = derive_terminated_by(counters, outcome)

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
    # ---- 自动审查层 P1 的三个落盘/回退读数（§6 字段表）------------------------
    #
    # 三者的口径都与「删不删」无关（设计稿在字段表里逐条写明）：
    # `on_disk` 是**产出**在盘上的路径数（删除也算一条），`not_empty` 是
    # 「有产出就必须看得见」，`revert_ok` 是「这一轮给不给得出可执行的整轮
    # 回退」。**不许**把 `not_empty` 写成「文件数少于运行前就报 false」。
    on_disk = changes_on_disk(change, sandbox)
    produced = bool(
        [
            item
            for item in (
                list(change.get("added") or [])
                + list(change.get("modified") or [])
                + list(change.get("removed") or [])
            )
            if not str(item).replace("\\", "/").startswith(_HARNESS_PREFIXES)
        ]
    )
    not_empty = (on_disk > 0) if produced else True
    revert_available = bool(counters["revert_commands"])
    # 「被拦下、等批复」与「被带进闸门」的分离：前者是 `blocked_actions`
    # （危险动作没执行），后者是 `actions_gated`（该拦就拦的 recall 读数）。
    blocked_actions = counters["blocked_actions"]
    blocked_by_review = counters["blocked_by_review"]
    # `blocked_but_ok`：**被阻断但验收绿**（§4 的误报代价必须可见）。判据是
    # 「拦它的是**没有高严重度发现**的触发条件（纯规模/回归）」，而不是
    # 「拦过它就都算」：L1 报了真缺陷而验收判据看不见，那是审查层的收益
    # （探针的 check 只覆盖它自己那几条判据），把它算成误报，这个数字就永远
    # 非零、也永远没人看（§4 明说「误报率必须度量，否则用户会学会忽略」）。
    blocked_but_ok = bool(counters["blocked_by_finding"] == 0) and bool(
        blocked_by_review
    ) and bool(check_ok)

    record = {
        "id": probe["id"],
        "shape": probe["shape"],
        "title": probe.get("title", ""),
        # goal 是**这一次实际发出去的那句话**（可能是变体）。判据一个字没动：
        # check.script 与 guard_hashes 都还是清单里那一份，见下面两条。
        "goal": variant["goal"],
        "goal_base": probe["goal"],
        "variant_index": variant["variant_index"],
        "variant_source": variant["variant_source"],
        "temperature": options["temperature"],
        "seed": options["seed"],
        "seed_base": seed_base,
        "repeat_index": repeat_index,
        "repeat_total": repeat_total,
        "check_script": str(check_script),
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
        # ---- 拆分（护栏落地后的口径）：总数 vs 其中被硬拦未执行的 --------------
        #
        # `repeat_blocks` 是**总数**（保持原义：检测到的原样重复调用次数）；
        # `repeat_blocks_blocked` 是其中**被护栏拦下、未真正执行**的次数；
        # `repeat_blocks_unblocked` 是两者之差，也就是**真正的空转**，
        # 下限 `max_repeat_blocks_unblocked` 判的就是它。
        # 读取类同口径。被拦的次数不套 0 线，但仍逐条记录。
        "repeat_blocks_blocked": counters["repeat_blocks_blocked"],
        "repeat_blocks_unblocked": (
            counters["repeat_blocks"] - counters["repeat_blocks_blocked"]
        ),
        "read_blocks_blocked": counters["read_blocks_blocked"],
        "read_blocks_unblocked": (
            counters["read_blocks"] - counters["read_blocks_blocked"]
        ),
        # 拦下之后的两条**可见性**读数（不是判定下限的计数）：
        # `blocked_refunds` = 因「整步都没执行」退还了几步（上限内）；
        # `combo_disabled` = 同参被拦到阈值后停用了几次（拦截换路了没有）。
        "blocked_refunds": counters["blocked_refunds"],
        "combo_disabled": counters["combo_disabled"],
        "supervisor_interventions": counters["supervisor_interventions"],
        "supervisor_false_claims": counters["supervisor_false_claims"],
        # 自动审查层 P0 的五个必填读数（判据口径见 design 的 §6 字段表）：
        # `false_completion` 是**派生**的（自认完成 + 验收红），其余四个来自事件流。
        "stale_references": counters["stale_references"],
        "tests_weakened": counters["tests_weakened"],
        "review_findings": counters["review_findings"],
        "false_completion": bool(counters["final_ok"] and not check_ok),
        "evidence_gaps": counters["evidence_gaps"],
        # ---- P1：风险分级放行 + 落盘/回退（判据口径见上面的字段表）----------
        "workspace_not_empty": bool(not_empty),
        "changes_on_disk": int(on_disk),
        "revert_available": bool(revert_available),
        "blocked_actions": int(blocked_actions),
        "blocked_by_review": int(blocked_by_review),
        "blocked_by_finding": int(counters["blocked_by_finding"]),
        "blocked_but_ok": bool(blocked_but_ok),
        "escalations": counters["escalations"],
        "escalated_before_action": counters["escalated_before_action"],
        "escalation_summary_complete": (
            counters["escalations"] > 0
            and counters["escalations_complete"] == counters["escalations"]
        ),
        "escalation_approved": counters["escalation_approved"],
        # ---- L3 语义审查（P2 后半，§6 字段表）--------------------------------
        #
        # `review_l3_ran` 是「L3 真的调用了模型几次」——**触发条件不满足时不跑**
        # 这件事靠它与 `verifier_ran` 的对比来证伪（设计稿 §6 P2 验收第 3 条：
        # 全量跑一遍后 `review_l3_ran` 的总次数要显著小于 `verifier_ran`）。
        # `l3_blocked` 只说明「L3 让完成声明降级」，**不是成果没落盘**。
        "review_l3_ran": counters["review_l3_ran"],
        "l3_findings": counters["l3_findings"],
        "l3_unverified": counters["l3_unverified"],
        "l3_blocked": counters["l3_blocked"],
        # §3.2.4 的比率口径：**按运行**算「每轮打断了用户几次」。
        # 目标 < 1 次/运行；≥ 0.5 视为设计失败，回去改触发条件。
        "escalation_rate": counters["escalations"],
        # recall = 命中 T1–T13 的动作里被拦下的比例（100% 才算对）。
        # 分母是「进了闸门的危险动作数」，分子是「其中真被拦下的」。
        "escalation_recall": (
            1.0
            if counters["actions_gated"] == 0
            else round(counters["blocked_actions"] / counters["actions_gated"], 3)
        ),
        # precision = 被拦的动作里能回指具名触发条件的比例。
        "escalation_precision": (
            1.0
            if counters["escalations"] == 0
            else round(
                len(
                    [
                        name
                        for name in counters["escalation_triggers"]
                        if name.startswith("T")
                    ]
                )
                / len(counters["escalation_triggers"]),
                3,
            )
        ),
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
        # ---- P0 指标（口径见 `parse_events` 里那两个字段的说明）---------------
        #
        # `terminated_by` **优先信主循环报出来的那一档**（它知道自己是撞了上限
        # 还是失败了）；主循环没报（老事件流 / 假 runner / 进程被杀）时按这一层
        # 能看见的事实补：超时 / 启动失败 / 进程异常退出 / 模型自认完成 /
        # 其余按「步数上限」。**不许**把「失败」写成「未完成」——那正是这次
        # 要修的那条静默（502 被记成未完成，收敛率里看不见它）。
        "concluded": bool(counters["concluded"]),
        "model_final": counters["model_final"],
        "terminated_by": terminated_by,
        "failed_reason": counters["failed_reason"],
        "tool_events": counters["tool_events"],
        "tool_failures": counters["tool_failures"],
        "blocked_total": counters["blocked_total"],
        "rewrite_refusals": counters["rewrite_refusals"],
        "guard_refreshes": counters["guard_refreshes"],
        "verifier_auto_ran": counters["verifier_auto_ran"],
        "verifier_final_ran": counters["verifier_final_ran"],
        "verifier_passed": counters["verifier_passed"],
        "verifier_failed": counters["verifier_failed"],
        # 非必填的细分读数：`review_findings` 按 kind 拆开，加一条「导入的本地
        # 模块不存在」。报告要能回答「是哪一类在刷屏」。
        "review_findings_by_kind": dict(counters["review_findings_by_kind"]),
        "broken_imports": counters["broken_imports"],
        # L3 的分档读数（非必填，但报告要能回答「哪一档在刷屏」）：
        # 严重度分布与「与记录冲突」的断言形状分布。
        "l3_findings_by_severity": dict(counters["l3_findings_by_severity"]),
        "l3_conflicts": dict(counters["l3_conflicts"]),
        # ---- P1 的分档读数（非必填，但报告要能回答「哪一条在刷屏」）---------
        "escalation_by_trigger": dict(counters["escalation_triggers"]),
        "actions_gated": counters["actions_gated"],
        "review_triggers": list(counters["review_triggers"]),
        "revert_commands": list(counters["revert_commands"]),
        "produced_changes": bool(produced),
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


def changes_on_disk(change, sandbox):
    """**已经落在磁盘上的产出路径数**（§6 字段表的口径）。

    三条口径都是设计稿写死的，实现不得走样：

    - **删除也算一条落到盘上的改动**：被删的文件进 `removed`，它是这一轮的
      产出（"我把这个文件合并掉了"），不是「文件数变少所以不算改动」。
    - **harness 自己写的东西不算产出**：`.agent/` 下是授权、检查点、索引、
      记忆库与事件流本身。算进来的话这个字段永远是正数，就废了。
    - 判的是「产出在不在盘上」，**不是「文件数有没有增加」**。
    """
    produced = list(change.get("added") or [])
    produced += list(change.get("modified") or [])
    produced += list(change.get("removed") or [])
    counted = []
    for item in produced:
        rel = str(item).replace("\\", "/")
        if rel.startswith(_HARNESS_PREFIXES):
            continue
        if rel not in counted:
            counted.append(rel)
    return len(counted)


def _unblocked_count(record, total_key, blocked_key, derived_key):
    """「未被拦的重复」读数：优先用记录里显式的派生字段，没有就现算。

    老记录（拆分之前）只有裸计数，构造出来的假记录也可能只给裸计数——
    那就按 `总数 - 被拦` 现算，缺 `blocked` 时当 0（= 全都没被拦，最严的读法）。
    """
    if record.get(derived_key) is not None:
        return record[derived_key]
    return record.get(total_key, 0) - record.get(blocked_key, 0)


#: 旧下限键 → 新下限键。**旧键保留、同义同线**：护栏引入之前，
#: `max_repeat_blocks` 的意思是「原样重试被拦 ≤ 0」；护栏自己就是拦截者之后，
#: 这条线**只对未被拦的重复生效**（被拦的那些是护栏在干活，不是空转）。
#: 读旧清单的工具不必改；新清单显式写 `*_unblocked`，口径写在名字里。
LEGACY_LIMIT_ALIASES = {
    "max_repeat_blocks": "max_repeat_blocks_unblocked",
    "max_read_blocks": "max_read_blocks_unblocked",
}


def evaluate_bounds(probe, record):
    """对照清单里的「预期下限」。命中不了的写清楚，不吞。"""
    expect = dict(probe.get("expect") or {})
    # 旧键折进新键：显式写了新键就按新键（它是口径的正式名字）。
    for legacy, current in LEGACY_LIMIT_ALIASES.items():
        if legacy in expect and current not in expect:
            expect[current] = expect[legacy]
    failed = []
    # 清单里的键（`max_*`）与记录里的键（裸计数）不是一个名字，这里显式对上。
    # 顺手写错过一次：直接拿 `max_read_blocks` 去 record 里取，取到 None，
    # 于是「拦读超上限」永远判成达标——单测把这条抓出来了。
    #
    # 两条「重复」线判的是**未被拦的**那部分（`*_unblocked`），不是总数：
    # 口径与理由写在 `benchmarks/probes/probes.json` 的 `_limits_note` 里。
    for record_key, expect_key, message in (
        (
            "repeat_blocks_unblocked",
            "max_repeat_blocks_unblocked",
            "未被拦的原样重试次数超上限",
        ),
        (
            "read_blocks_unblocked",
            "max_read_blocks_unblocked",
            "未被拦的读取被拦次数超上限",
        ),
        (
            "supervisor_false_claims",
            "max_supervisor_false_claims",
            "督导说错事实的次数超上限",
        ),
        # 自动审查层 P0 的读数也走下限（清单里没写这几条时它们只是被记录）：
        # 判据话术与阈值由 P1 标定，P0 只把旋钮接上。
        ("stale_references", "max_stale_references", "残留引用处数超上限"),
        ("tests_weakened", "max_tests_weakened", "测试被改弱的条数超上限"),
        ("evidence_gaps", "max_evidence_gaps", "没有证据的完成声明条数超上限"),
        ("review_findings", "max_review_findings", "审查发现条数超上限"),
        # ---- P1：风险分级放行的阈值同样走下限/上限（§6 字段表）--------------
        (
            "blocked_actions",
            "max_blocked_actions",
            "被拦下的危险动作数超上限",
        ),
        ("escalations", "max_escalations", "主动请示次数超上限"),
        # ---- L3 语义审查（P2 后半，§6 字段表）--------------------------------
        # 发现条数走**上限**（§4 的噪声控制：一次审查的发现条数设上限，超出说明
        # 改动太大、应当拆）；`review_l3_ran` 走**下限**那一侧由探针清单用
        # `min_review_l3_ran` 表达（「这一轮该跑 L3 却没跑」是缺陷）。
        ("l3_findings", "max_l3_findings", "L3 语义审查的发现条数超上限"),
        ("l3_unverified", "max_l3_unverified", "L3 被事实核对降级的条数超上限"),
    ):
        if expect_key not in expect:
            continue
        limit = expect[expect_key]
        if record_key == "repeat_blocks_unblocked":
            value = _unblocked_count(
                record, "repeat_blocks", "repeat_blocks_blocked", record_key
            )
        elif record_key == "read_blocks_unblocked":
            value = _unblocked_count(
                record, "read_blocks", "read_blocks_blocked", record_key
            )
        else:
            value = record.get(record_key, 0)
        if value > limit:
            failed.append(f"{message}：{value} > {limit}")
    # §2.5 的三个字段走两条线：`workspace_not_empty` / `revert_available`
    # 是**布尔下限（必须为真）**，`changes_on_disk` 走**下限**（有产出时必须 > 0）。
    # 判据口径只关于「落盘 / 可回退 / 可列出」，**与删不删无关**（§6 字段表）。
    if expect.get("require_workspace_not_empty") and not record.get(
        "workspace_not_empty", True
    ):
        failed.append(
            "本轮产出过改动，但盘上看不到它们（成果被扣在系统内部；"
            "注意这条判的是落盘，不是文件数增减）"
        )
    if expect.get("require_revert_available") and not record.get(
        "revert_available", True
    ):
        failed.append("这一轮给不出可执行的整轮回退命令")
    if expect.get("min_changes_on_disk") is not None:
        floor = expect["min_changes_on_disk"]
        value = record.get("changes_on_disk", 0)
        if value < floor:
            failed.append(f"落在盘上的产出路径数不足：{value} < {floor}")
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
        # 两个数一起报：被护栏拦下的（我们扣的分）与未被拦的（真病态）。
        parts.append(
            f"原样重复 {record['repeat_blocks']} 次"
            f"（被护栏拦下未执行 {record.get('repeat_blocks_blocked', 0)} 次，"
            f"未被拦 {record.get('repeat_blocks_unblocked', 0)} 次）"
        )
    if record["read_blocks"]:
        parts.append(
            f"读取类重复 {record['read_blocks']} 次"
            f"（被拦未执行 {record.get('read_blocks_blocked', 0)} 次，"
            f"未被拦 {record.get('read_blocks_unblocked', 0)} 次；被拦次数高不必然是死锁）"
        )
    if record.get("blocked_refunds"):
        parts.append(
            f"因被拦退还步数 {record['blocked_refunds']} 次"
            "（拦下的调用没有执行，不算有效推进）"
        )
    if record.get("combo_disabled"):
        parts.append(f"组合停用 {record['combo_disabled']} 次（同参被拦到阈值）")
    if record["rewrite_refusals"]:
        parts.append(f"整文件重写被拒 {record['rewrite_refusals']} 次")
    if record["supervisor_interventions"]:
        parts.append(f"督导介入 {record['supervisor_interventions']} 次")
    if record["supervisor_false_claims"]:
        parts.append(f"督导说错事实 {record['supervisor_false_claims']} 次")
    # 自动审查层 P0 的读数（有才说：全是 0 时加四句噪声，没人会读）。
    if record.get("stale_references"):
        parts.append(f"残留引用 {record['stale_references']} 处")
    if record.get("tests_weakened"):
        parts.append(f"测试被改弱 {record['tests_weakened']} 条")
    if record.get("evidence_gaps"):
        parts.append(f"无证据的完成声明 {record['evidence_gaps']} 条")
    if record.get("false_completion"):
        parts.append("假完成（自认完成而验收红）")
    # L3 语义审查（P2 后半）：只在真的跑过、或真的拦住过东西时才说
    # （`review_l3_ran` 为 0 是**正常的**——触发条件不满足就不跑）。
    if record.get("review_l3_ran"):
        parts.append(f"L3 语义审查跑了 {record['review_l3_ran']} 次")
        if record.get("l3_findings"):
            severity = record.get("l3_findings_by_severity") or {}
            detail = "、".join(f"{key} {value}" for key, value in severity.items())
            parts.append(
                f"L3 发现 {record['l3_findings']} 条" + (f"（{detail}）" if detail else "")
            )
        if record.get("l3_unverified"):
            parts.append(f"L3 有 {record['l3_unverified']} 条与记录不符已降级")
        if record.get("l3_blocked"):
            parts.append("L3 拦下了完成声明（成果照落盘）")
    # 自动审查层 P1 的读数。措辞刻意与「删不删」无关：这几条量的都是
    # 落盘 / 可回退 / 该不该拦（§2.5 与 §6 字段表）。
    if record.get("escalations"):
        triggers = "、".join(sorted((record.get("escalation_by_trigger") or {}).keys()))
        parts.append(
            f"主动请示 {record['escalations']} 次（{triggers or '无触发条件'}）"
        )
    if record.get("blocked_by_review"):
        parts.append(f"「不许宣称完成」判了 {record['blocked_by_review']} 次")
    if record.get("blocked_actions"):
        parts.append(f"危险动作被拦下 {record['blocked_actions']} 个（等批复）")
    if record.get("blocked_but_ok"):
        parts.append("被阻断但验收绿（误报代价，要单列）")
    if record.get("revert_commands"):
        parts.append("可回退（报告里给了 spool revert 命令）")
    elif record.get("produced_changes"):
        parts.append("有产出但没给出回退命令")
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
# 聚合：带扰动的重复
# --------------------------------------------------------------------------


def _spread(values):
    """一组数的 min/max/均/极差。

    `None` 一律**剔除**，不当 0：超时或没跑完的那一次确实没报出步数，
    把它当 0 会让极差凭空变大，也会把均值拽下去——那是「没数据」，
    不是「很稳定/很不稳定」。一组全空就如实返回 count=0。
    """
    numbers = [
        float(value)
        for value in values
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    if not numbers:
        return {"count": 0, "min": None, "max": None, "mean": None, "span": None}
    return {
        "count": len(numbers),
        "min": min(numbers),
        "max": max(numbers),
        "mean": round(sum(numbers) / len(numbers), 3),
        "span": round(max(numbers) - min(numbers), 3),
    }


def attempt_passed(record):
    """这一次算不算「过」：**判据绿 且 下限过**。

    两个都要。只看判据会漏掉「交出来的东西对了、但过程中被拦了 3 次」——
    那是运气好，不是稳定；只看下限会漏掉「一路顺风但没做对」。
    跳过的记录不是一次尝试，永远不算过。
    """
    if record.get("skipped"):
        return False
    return bool(record.get("check_ok")) and bool(record.get("bounds_ok"))


def aggregate_stability(records):
    """按探针聚合重复读数：通过率 + 各指标的 min–max–均。

    口径写在返回的每个字段里，谁都能复算：
    - `attempts`：真的跑了的次数（跳过的**不算**分母，也不算分子）；
    - `passed`：其中「判据绿 且 下限过」的次数；`pass_rate = passed / attempts`；
    - `metrics`：每个指标的 `count/min/max/mean/span`，只统计报出了数字的那几次；
    - `failures`：每一次没过的**现场清单**（含沙箱与事件流路径，失败轨迹从那里读）。
    """
    grouped = {}
    order = []
    for record in records:
        identifier = record.get("id") or "?"
        if identifier not in grouped:
            grouped[identifier] = []
            order.append(identifier)
        grouped[identifier].append(record)

    summary = {}
    for identifier in order:
        items = grouped[identifier]
        attempts = [item for item in items if not item.get("skipped")]
        passed = [item for item in attempts if attempt_passed(item)]
        failures = []
        for item in attempts:
            if attempt_passed(item):
                continue
            failures.append(
                {
                    "repeat_index": item.get("repeat_index"),
                    "temperature": item.get("temperature"),
                    "seed": item.get("seed"),
                    "variant_index": item.get("variant_index"),
                    "variant_source": item.get("variant_source"),
                    "goal": item.get("goal"),
                    "steps": item.get("steps"),
                    "wall_seconds": item.get("wall_seconds"),
                    "final_ok": item.get("final_ok"),
                    "check_ok": item.get("check_ok"),
                    "bounds_ok": item.get("bounds_ok"),
                    "timed_out": item.get("timed_out"),
                    "notes": item.get("notes", ""),
                    "sandbox": item.get("sandbox", ""),
                    "events_path": item.get("events_path", ""),
                    "check_output_tail": (item.get("check_output") or "")[-400:],
                }
            )
        summary[identifier] = {
            "attempts": len(attempts),
            "skipped": len(items) - len(attempts),
            "passed": len(passed),
            "pass_rate": (
                round(len(passed) / len(attempts), 4) if attempts else None
            ),
            "checks_green": sum(1 for item in attempts if item.get("check_ok")),
            "bounds_ok": sum(1 for item in attempts if item.get("bounds_ok")),
            "finished": sum(1 for item in attempts if item.get("final_ok")),
            "unstable": bool(attempts) and len(passed) != len(attempts),
            "temperatures": sorted(
                {item.get("temperature") for item in attempts}, key=str
            ),
            "seeds": sorted({item.get("seed") for item in attempts}, key=str),
            "variant_sources": sorted(
                {item.get("variant_source") for item in attempts}, key=str
            ),
            "metrics": {
                key: _spread([item.get(key) for item in attempts])
                for key in STABILITY_METRICS
            },
            "failures": failures,
        }
    return summary


def _metric_cell(spread):
    if not spread or spread["count"] == 0:
        return "—"
    if spread["count"] == 1:
        return f"{spread['min']:.1f}"
    return f"{spread['min']:.1f}-{spread['max']:.1f}-{spread['mean']:.1f}"


def render_stability(records):
    """给人看的稳定度表：一行一个探针。格式是 `min-max-均`。"""
    stability = aggregate_stability(records)
    header = (
        f"{'探针':<18}{'采样':>4}{'过':>4}{'通过率':>8}"
        f"{'步数':>14}{'墙钟s':>20}{'输入tok':>26}{'输出tok':>24}{'不稳定':>8}"
    )
    lines = [
        "稳定度（通过率 = 判据绿 且 下限过；指标写 min-max-均，只统计真的跑了的次数）：",
        header,
        "-" * len(header),
    ]
    for identifier, item in stability.items():
        rate = "—" if item["pass_rate"] is None else f"{item['pass_rate']:.2f}"
        metrics = item["metrics"]
        lines.append(
            f"{identifier:<18}{item['attempts']:>4}{item['passed']:>4}{rate:>8}"
            f"{_metric_cell(metrics['steps']):>14}"
            f"{_metric_cell(metrics['wall_seconds']):>20}"
            f"{_metric_cell(metrics['prompt_tokens']):>26}"
            f"{_metric_cell(metrics['completion_tokens']):>24}"
            f"{'是' if item['unstable'] else '否':>8}"
        )
        if item["skipped"]:
            lines.append(f"{'':<18}（另有 {item['skipped']} 次因预算不足被跳过）")
    return "\n".join(lines)


def render_failures(records):
    """失败轨迹的入口清单：哪一次、什么设置、现场在哪儿、判据说了什么。

    真正的轨迹（工具调用与拦截）在 `events_path` 指向的 JSONL 里，这里只把
    「去哪儿读」和「判据的最后一句话」摆出来——聚合里塞全文只会把它淹没。
    """
    stability = aggregate_stability(records)
    lines = []
    for identifier, item in stability.items():
        for failure in item["failures"]:
            lines.append(
                f"[{identifier}] 第 {failure['repeat_index']} 次（温度 {failure['temperature']}，"
                f"种子 {failure['seed']}，变体 {failure['variant_index']}）"
                f"步={failure['steps']} 墙钟={failure['wall_seconds']}s "
                f"判据={'绿' if failure['check_ok'] else '红'} "
                f"下限={'过' if failure['bounds_ok'] else '未过'}"
            )
            lines.append(f"    说明：{failure['notes']}")
            lines.append(f"    事件流：{failure['events_path']}")
            tail = " ".join((failure["check_output_tail"] or "").split())
            if tail:
                lines.append(f"    判据尾部：{tail[-300:]}")
    if not lines:
        return "没有不过的尝试。"
    return "不过的尝试（失败轨迹从事件流里读）：\n" + "\n".join(lines)


# --------------------------------------------------------------------------
# 落盘与展示
# --------------------------------------------------------------------------


def save_record(record, stamp):
    RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    # 重复多次时文件名要带上是第几次：同名会把上一次的记录覆盖掉，
    # 而「同一设置下的 N 次读数」正是这一层要留下的东西。
    name = f"{stamp}-{record['id']}"
    if int(record.get("repeat_total") or 1) > 1:
        name += f"-r{record.get('repeat_index', 0)}"
    path = RECORDS_DIR / f"{name}.json"
    path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with RESULTS_JSONL.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def save_summary(records, meta, stability=None, path=None):
    """汇总落盘。`stability` 段就是「带扰动的重复」那一层的结论。"""
    payload = {
        "meta": meta,
        "stability": aggregate_stability(records) if stability is None else stability,
        "probes": records,
    }
    target = Path(path or SUMMARY_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target


def _cell(value, width=None):
    text = "" if value is None else str(value)
    if width:
        text = text[:width]
    return text


def render_table(records):
    """一行一个探针的汇总表。列就是报告里要念的那些数字。

    「重复 / 拦读」两组各给三个数：**总数**、其中**被护栏拦下未执行**、
    其中**未被拦**（真正的空转，下限判的就是它）。前两个数合起来才看得清
    「分是我们扣的」还是「它真的在打转」。
    """
    header = (
        f"{'探针':<18}{'形状':<18}{'结局':<10}{'步':>3}{'调用':>5}"
        f"{'输入tok':>9}{'输出tok':>9}{'墙钟s':>8}{'验证器':>8}"
        f"{'重复':>5}{'被拦':>5}{'未拦':>5}{'拦读':>5}{'被拦':>5}{'未拦':>5}"
        f"{'退还':>5}{'停用':>5}{'督导':>5}"
        f"{'督导错':>7}{'判据':>6}{'下限':>6}"
    )
    lines = [header, "-" * len(header)]
    for record in records:
        if record.get("skipped"):
            lines.append(
                f"{record['id']:<18}{_cell(record.get('shape'), 18):<18}"
                f"{'跳过':<10}{'—':>3}{'—':>5}{'—':>9}{'—':>9}"
                f"{record.get('wall_seconds', 0):>8.1f}{'—':>8}{'—':>5}{'—':>5}{'—':>5}"
                f"{'—':>5}{'—':>5}{'—':>5}{'—':>5}{'—':>5}{'—':>5}{'—':>7}{'—':>6}{'—':>6}"
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
            f"{record['repeat_blocks']:>5}"
            f"{_cell(record.get('repeat_blocks_blocked', 0), 5):>5}"
            f"{_cell(record.get('repeat_blocks_unblocked', 0), 5):>5}"
            f"{record['read_blocks']:>5}"
            f"{_cell(record.get('read_blocks_blocked', 0), 5):>5}"
            f"{_cell(record.get('read_blocks_unblocked', 0), 5):>5}"
            f"{_cell(record.get('blocked_refunds', 0), 5):>5}"
            f"{_cell(record.get('combo_disabled', 0), 5):>5}"
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
    note = manifest.get("limits_note") or []
    if note:
        lines.append("下限口径说明（清单里的 _limits_note）：")
        lines.extend(f"  · {item}" for item in note)
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
    parser.add_argument(
        "--check-env",
        action="store_true",
        help="只做测量环境自证（rg / 代理 / httpx 代理防御）并退出；不合格退出码 3",
    )
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
        "--temperature",
        type=float,
        default=None,
        help=(
            "采样温度。不给=清单 defaults 或内置默认 0（确定性基线）；"
            "抬起来才测得到波动——温度 0 下重复 N 次只能证明「可复现」"
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help=(
            "采样种子的**基准**：第 k 次重复用 基准+k（每一次独立采样，"
            "每一次的种子都落记录）。温度 > 0 时给它，同一次跑分才可复现。"
            "不给=服务端自己取"
        ),
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="每个探针重复几次（默认 1）。每次一条记录，带上是第几次与当次的设置",
    )
    parser.add_argument(
        "--no-variants",
        action="store_true",
        help="不轮换 goal_variants：每次都只用清单里的目标原话（对照组）",
    )
    parser.add_argument(
        "--total-budget",
        type=int,
        default=0,
        help=(
            "--all 的总墙钟上限（秒）；预算不够时先减少重复次数，"
            "连一次都跑不下才记为跳过，不偷偷跑下去"
        ),
    )
    parser.add_argument(
        "--no-save", action="store_true", help="只打印，不落记录（自检/单测用）"
    )
    parser.add_argument(
        "--summary-out",
        default="",
        help=(
            "把这一次的汇总（含 stability 段）另存一份；三种设置各存一份，"
            "否则后跑的会把前一份 summary.json 覆盖掉。默认仍更新 summary.json"
        ),
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
    if args.check_env:
        code, _ = env_self_check(window=args.window, stream=sys.stdout)
        return code
    if not args.all and not args.probe:
        parser.print_help()
        return 2

    options = resolve_options(manifest, args)
    root = Path(args.root).expanduser().resolve() if args.root else default_root()
    budget = args.total_budget or int(
        manifest["defaults"].get("total_budget_seconds") or 0
    )
    repeats = max(1, int(args.repeat or manifest["defaults"].get("repeat") or 1))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    python = repo_python()

    # 开跑前自证一次：环境不合格就不买这一轮读数（详见 envcheck 模块头）。
    code, env_report = env_self_check(
        window=int(options.get("window") or args.window or 0)
    )
    if code:
        print("测量环境不合格：这一批不开跑（跑完再解释，token 已经花掉了）。", file=sys.stderr)
        return code
    if env_report.path_added:
        print("测量环境自证：补进 PATH " + "；".join(env_report.path_added), file=sys.stderr)

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
    print(
        f"采样设置：温度 {options['temperature']}，"
        f"种子 {'（服务端自己取）' if options['seed'] is None else options['seed']}"
        f"；每个探针重复 {repeats} 次"
        + ("；变体轮换开" if options["rotate_variants"] else "；只用目标原话（--no-variants）")
    )
    print(f"解释器：{python}")
    print("")

    records = []
    batch_started = time.time()
    for probe in selected:
        # 每个探针重复 repeats 次；预算按**每一次**检查，快撞线时先减少重复次数，
        # 而不是把整条探针丢掉——「少采几次」比「一个数都没有」有用得多。
        for repeat_index in range(repeats):
            elapsed = time.time() - batch_started
            remaining = budget - elapsed if budget else None
            if remaining is not None and remaining < 60:
                record = skipped_record(
                    probe, options, repeat_index, repeats, budget, elapsed
                )
                records.append(record)
                print(
                    f"[{probe['id']}] 第 {repeat_index + 1}/{repeats} 次跳过：整批预算不足"
                    f"（已用 {elapsed:.0f}s / {budget}s）"
                )
                if not args.no_save:
                    save_record(record, stamp)
                break

            # 单条也不许把整批预算冲破：取「本探针上限」与「剩余预算」里更小的那个。
            local = dict(options)
            if remaining is not None:
                local["timeout"] = int(max(60, min(options["timeout"], remaining)))
            variant = goal_for_attempt(
                probe, repeat_index, rotate=options["rotate_variants"]
            )
            label = (
                f"[{probe['id']}] 第 {repeat_index + 1}/{repeats} 次开跑（{probe['shape']}；"
                f"温度 {options['temperature']}；"
                f"种子 {seed_for_attempt(options['seed'], repeat_index)}；"
                + (
                    "目标原话"
                    if variant["variant_index"] is None
                    else f"变体 #{variant['variant_index']}"
                )
                + "）…"
            )
            print(label)
            record = run_one(
                probe,
                manifest,
                local,
                root,
                runner=runner,
                python=python,
                repeat_index=repeat_index,
                repeat_total=repeats,
                variant=variant,
            )
            records.append(record)
            print(
                f"[{probe['id']}] 第 {repeat_index + 1}/{repeats} 次 exit={record['exit_code']} "
                f"步={record['steps']} 墙钟={record['wall_seconds']:.1f}s "
                f"验收={'绿' if record['check_ok'] else '红'} "
                f"下限={'过' if record['bounds_ok'] else '未过'} "
                # P0：结论与终止者印在每一行上。失败那一条**必须刺眼**——
                # 它以前与「未完成」长得一样，整段 502 因此在读数里消失。
                f"结论={'有' if record.get('concluded') else '无'} "
                f"终止={record.get('terminated_by') or '—'}"
                + (f" ！！失败：{record['failed_reason'][:80]}"
                   if record.get("failed_reason") else "")
            )
            if not args.no_save:
                save_record(record, stamp)

    total_wall = round(time.time() - batch_started, 1)
    table = render_table(records)
    stability = aggregate_stability(records)
    stability_table = render_stability(records)
    failure_notes = render_failures(records)
    totals = {
        "probes": len(records),
        "attempts": sum(item.get("attempts", 0) for item in stability.values()),
        "passed": sum(item.get("passed", 0) for item in stability.values()),
        "finished": sum(1 for item in records if item.get("final_ok")),
        "check_ok": sum(1 for item in records if item.get("check_ok")),
        "bounds_ok": sum(1 for item in records if item.get("bounds_ok")),
        "skipped": sum(1 for item in records if item.get("skipped")),
        # ---- P0：收敛率与终止者分布（口径见记录里那两个字段）------------------
        #
        # `concluded` 的分母**只算真跑过的那几次**（跳过的那几次没跑，算进去会
        # 把收敛率凭空抬上去）。`terminated_by` 按档计数，失败与超时各占一档：
        # 它们不是「模型没做完」，混进同一个数里就永远看不出环境坏了。
        "concluded": sum(
            1 for item in records if item.get("concluded") and not item.get("skipped")
        ),
        "attempts_concluded": sum(1 for item in records if not item.get("skipped")),
        "terminated_by": _tally_terminated(records),
        "failed": sum(
            1 for item in records if item.get("terminated_by") == "失败"
        ),
        "steps": sum(item.get("steps") or 0 for item in records),
        "model_calls": sum(item.get("model_calls") or 0 for item in records),
        "prompt_tokens": sum(item.get("prompt_tokens") or 0 for item in records),
        "completion_tokens": sum(item.get("completion_tokens") or 0 for item in records),
        # ---- 拦截拆分：让「我们扣的分」与「真病态」在同一行里可分辨 ----------
        # `blocked_*` 是护栏拦下、**没有执行**的次数（它由护栏自己的上限约束）；
        # `unblocked_*` 是护栏没拦住的重复，也就是真正的空转——下限判的是它。
        "repeat_blocked": sum(item.get("repeat_blocks_blocked") or 0 for item in records),
        "repeat_unblocked": sum(item.get("repeat_blocks_unblocked") or 0 for item in records),
        "read_blocked": sum(item.get("read_blocks_blocked") or 0 for item in records),
        "read_unblocked": sum(item.get("read_blocks_unblocked") or 0 for item in records),
        # 失分构成：只输在「未被拦的重复」这一条上的尝试数（真病态）。
        "attempts_unblocked_repeat": sum(
            1
            for item in records
            if not item.get("skipped")
            and (item.get("repeat_blocks_unblocked") or 0) > 0
        ),
    }
    summary_line = (
        "合计：记录 {probes} 条，真跑的采样 {attempts} 次，"
        "「判据绿 且 下限过」{passed} 次；完成 {finished}/{probes}，"
        "验收绿 {check_ok}/{probes}，下限达标 {bounds_ok}/{probes}，跳过 {skipped} 条，"
        "步数 {steps}，模型调用 {model_calls}，"
        "token 输入 {prompt_tokens} / 输出 {completion_tokens}\n"
        "拦截拆分：重复 {repeat_blocked} 次被拦（未执行）+ {repeat_unblocked} 次未被拦，"
        "读取类 {read_blocked} 次被拦 + {read_unblocked} 次未被拦；"
        "未被拦重复非零的尝试 {attempts_unblocked_repeat} 次（下限只看未被拦的那部分）\n"
        "P0 收敛：写出结论 {concluded}/{attempts_concluded} 次"
        "（分母只算真跑过的那几次）；终止分布 {terminated_by}；"
        "失败 {failed} 次（失败不是「没做完」——它单独一档）"
    ).format(**totals)
    wall_line = f"整批墙钟：{total_wall}s" + (f"（预算上限 {budget}s）" if budget else "")
    setting_line = (
        f"设置：温度 {options['temperature']}，"
        f"种子 {'（服务端自己取）' if options['seed'] is None else options['seed']}，"
        f"每探针 {repeats} 次"
        + ("，变体轮换" if options["rotate_variants"] else "，固定目标原话")
    )

    print("")
    print(table)
    print("")
    print(stability_table)
    print("")
    print(failure_notes)
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
                    setting_line,
                    wall_line,
                    "",
                    table,
                    "",
                    stability_table,
                    "",
                    failure_notes,
                    "",
                    summary_line,
                    "",
                    "逐条说明：",
                    *[
                        f"  {item['id']} 第 {item.get('repeat_index', 0) + 1}/"
                        f"{item.get('repeat_total', 1)} 次：{item.get('notes', '')}"
                        for item in records
                    ],
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
            "temperature": options["temperature"],
            "seed": options["seed"],
            "repeat": repeats,
            "rotate_variants": options["rotate_variants"],
            "total_wall_seconds": total_wall,
            "budget_seconds": budget,
            "within_budget": (not budget) or total_wall <= budget,
            "totals": totals,
        }
        if args.all or args.summary_out:
            if args.summary_out:
                path = save_summary(records, meta, stability, args.summary_out)
                print(f"汇总已落盘：{path}")
                if Path(args.summary_out).resolve() != SUMMARY_PATH.resolve():
                    # summary.json 仍然表示「最近一次」，否则三种设置跑完只剩最后一份。
                    save_summary(records, meta, stability, SUMMARY_PATH)
            else:
                path = save_summary(records, meta, stability)
                print(f"汇总已落盘：{path}")
        print(f"逐条记录目录：{RECORDS_DIR}")
        print(f"追加表：{RESULTS_JSONL}")

    return 0 if all(
        attempt_passed(item) for item in records if not item.get("skipped")
    ) and any(not item.get("skipped") for item in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
