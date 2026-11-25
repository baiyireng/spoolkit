"""`spool memory migrate`：预演报告、`--yes` 的备份/重写、边界。

这一批用例钉的是计划 Task 6 的五条验收：报告四栏与理由、`--yes` 的备份
**逐字节**等于原文、重名不覆盖、空工作区不建文件、**dry-run 不读工作区
`.env`**、**dry-run 只读打开库**。第三条（`--yes` 先备份后写）是四条变异的
第一条的靶子：把顺序倒过来这条就红。
"""

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from spoolkit.cli.app import main
from spoolkit.cli.commands import memory as memory_cmd
from spoolkit.memory.hot import read_hot

GREETING = "你好！我是你的本地编程助手。有什么代码问题、文件改动或需要跑测试的任务，尽管交给我。"
RETELL = "你之前问过两次同样的问题：请找出本工作区里的报销单.txt，告诉我完整路径。"
_WEATHER_CITIES = (
    "北京", "上海", "广州", "深圳", "成都", "杭州", "武汉", "西安",
    "南京", "重庆", "天津", "苏州", "青岛", "长沙", "厦门", "沈阳",
)
# 16 城市的天气表：**一条物理行**、400 余字——与真实工作区 `memory.md:7`
# 同一形状（那边 401 字，也是靠长度线被 J4 判掉的）。语料前提由
# `test_三条寒暄与表格答复的判据分类` 里的 `len(WEATHER) > 400` 钉住。
WEATHER = (
    "以下是今天（2026-09-29）中国主要城市的天气情况："
    + "".join(
        f" | {city} | {17 + index}°C | {25 + index}°C | 多云转晴 |"
        for index, city in enumerate(_WEATHER_CITIES)
    )
)
DELIVERY = (
    "贪吃蛇小游戏已经实现完毕，文件为 `snake.py`，语法检查通过。 "
    "**运行方式：** ``` python snake.py ``` **操作说明：** - 方向键 / WASD 控制蛇的移动方向"
)
WRAP_UP = "任务已完成。snake.html 已包含所有要求的修改（碰撞边界、输入队列），请确认最终文件状态并宣告完成。"
# 一次性收尾里的"请确认"与天气表的表格形状要分开钉：这条**不含**表格，
# 只靠 J1 的"请确认"命中，所以它进的是 J1 那一栏。
EXTRA_DELIVERY = (
    "网页版贪吃蛇已经写好了，文件为 `snake.html`。**运行方式：** 用浏览器打开 "
    "`snake.html` 即可（双击或拖进浏览器窗口）。**操作说明：** - 方向键 / WASD 控制"
)
FACT = "测试命令是 pytest -q"

# 与真实工作区同一形状的语料：3 条逐字相同 + 1 条复述 + 1 张天气表 +
# 2 段交付正文 + 1 条一次性收尾 + 1 条真事实 = **9 条**（与真实工作区同为 9 条）。
# **每条都是一条物理行**（真实 `memory.md` 也是这样：列表项各自一行，
# `parse_hot` 只认 `- ` 开头的行），所以这里的备注写的行号就是报告里的行号。
SAMPLE_LINES = [
    "# 项目记忆（自动维护，也可手动编辑）",  # 1
    "",  # 2
    "## 项目事实",  # 3
    f"- {GREETING}",  # 4 → J3
    f"- {GREETING}",  # 5 → duplicate（锚点 :4）
    f"- {RETELL}",  # 6 → J1
    f"- {WEATHER}",  # 7 → J4（天气表超长）
    f"- {GREETING}",  # 8 → duplicate（锚点 :4）
    f"- {DELIVERY}",  # 9 → J4（代码围栏）
    f"- {EXTRA_DELIVERY}",  # 10 → J4（交付模板小标题）
    f"- {WRAP_UP}",  # 11 → J1（请确认）
    f"- {FACT}",  # 12 → 收进热记忆
]


def _workspace(tmp_path: Path, name: str = "ws", lines: list[str] | None = None) -> Path:
    """造一个工作区；`memory.md` 的每一行就是 `lines` 里的那一行。

    **不加尾换行**：加了它 `splitlines()` 会多出一条空行，本文件里那些
    "`# 4 → J3`" 的行号备注就全错位了（报告的行号必须与文件真实行号一致）。
    """
    root = tmp_path / name
    agent = root / ".agent"
    agent.mkdir(parents=True)
    (agent / "memory.md").write_text(
        "\n".join(SAMPLE_LINES if lines is None else lines), encoding="utf-8"
    )
    return root


def _bytes(path: Path) -> bytes:
    return path.read_bytes()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _listing(root: Path) -> list[str]:
    agent = root / ".agent"
    return sorted(item.name for item in agent.iterdir())


# ---- 验收 1：dry-run 的四栏报告与"什么都没写" --------------------------------


def test_dry_run报告四栏与逐条理由正确(tmp_path: Path, capsys) -> None:
    root = _workspace(tmp_path)
    before_md = _bytes(root / ".agent" / "memory.md")
    before_listing = _listing(root)

    assert main(["memory", "migrate", "--root", str(root)]) == 0

    out = capsys.readouterr().out
    # 解析 9 条（与真实工作区同为 9 条），行号与文件真实行号一致。
    assert "解析：9 条" in out
    assert "memory.md:4" in out and "memory.md:12" in out
    # 真事实一条（`:12`）；`:7` 的天气表超长，已被 J4 降级。
    assert "收进热记忆（1 条）" in out
    # 判重 / 判据拒 8 条：判据拒 6 条（J3 :4 / J1 :6 / J4 :7 / J4 :9 / J4 :10 /
    # J1 :11）+ duplicate 2 条（:5、:8，锚点 :4）。
    assert "判重 / 判据拒（8 条）" in out
    assert "[J3] memory.md:4" in out
    assert "[duplicate] memory.md:5" in out
    assert "[duplicate] memory.md:8" in out
    assert "与 memory.md:4 规范化后相同" in out
    # 锚点自己也没入库（J3）——不写这句，读报告的人会以为留下了一条。
    assert "锚点自己也没入库" in out
    # 降级 6 条：J1(:6 复述用户) / J4(:7 天气表长度) / J4(:9 围栏) /
    #            J4(:10 交付小标题) / J1(:11 请确认) / 以及 :4 的寒暄（J3）
    assert "降级为 episode（6 条）" in out
    # 未判 J2 必须显式写出（计划「风险 10」）。
    assert "未判：J2（evidence）" in out
    assert "本报告不据此判任何一条" in out

    # dry-run 什么都没写：文件逐字节不变、目录无新增（尤其没有 .bak）。
    assert _bytes(root / ".agent" / "memory.md") == before_md
    assert _listing(root) == before_listing


def test_dry_run是默认行为_不给yes就不动文件(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    before = _bytes(root / ".agent" / "memory.md")

    assert main(["memory", "migrate", "--dry-run", "--root", str(root)]) == 0

    assert _bytes(root / ".agent" / "memory.md") == before
    assert not list((root / ".agent").glob("*.bak-*"))


def test_每条条目的行号与文件真实行号一致(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    report = memory_cmd.analyze(root)
    by_line = {item.line: item.text for item in report.items}
    for dup in report.duplicates:
        by_line[dup.item.line] = dup.item.text

    raw = (root / ".agent" / "memory.md").read_text(encoding="utf-8").splitlines()
    assert sorted(by_line) == [4, 5, 6, 7, 8, 9, 10, 11, 12]
    for line, text in by_line.items():
        assert raw[line - 1].strip() == f"- {text}".strip()


def test_逐字相同的条目各报自己的行号(tmp_path: Path) -> None:
    """三条一模一样的寒暄必须报成三个行号（`:4`、`:5`、`:8`），不能全指 `:4`。

    这是"一行一条 + 按顺序排除已占用的行"的口径：只把第一条当锚点，后面
    两条各自报到自己那一行——读报告的人才能回文件里核对。
    """
    root = _workspace(tmp_path)
    report = memory_cmd.analyze(root)

    lines = sorted([item.line for item in report.items] + [d.item.line for d in report.duplicates])
    assert lines == [4, 5, 6, 7, 8, 9, 10, 11, 12]
    assert report.items[0].line == 4
    assert sorted(dup.item.line for dup in report.duplicates) == [5, 8]
    assert {dup.anchor_line for dup in report.duplicates} == {4}


def test_原文里对不上的条目退回上一条的行号加一(tmp_path: Path) -> None:
    """文件被手工改坏（解析出的条目在原文里找不到）时不崩，也不静默丢条目。"""
    root = _workspace(tmp_path)
    md = root / ".agent" / "memory.md"
    # `parse_hot` 与原文都还认这两条，但把正文改得对不上解析结果：
    # 直接调 `_find_lines` 造这个现场（等价于"文件在手改中途"）。
    assert memory_cmd._find_lines("## 项目事实\n- 甲\n- 乙\n", ["丙", "丁"]) == [2, 3]


# ---- 判据：3 条寒暄被拒、其余降级、真事实留下 ---------------------------------


def test_三条寒暄与表格答复的判据分类(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    report = memory_cmd.analyze(root)

    verdicts = {item.line: item.rule_id for item in report.items}
    assert verdicts[4] == "J3"  # 寒暄/自述
    assert verdicts[6] == "J1"  # 复述用户（"你之前问过"→ 一次性指代）
    assert verdicts[7] == "J4"  # 12 城市天气表：401 字，超 remember_max_chars
    assert verdicts[9] == "J4"  # 交付正文（代码围栏与正文同行）
    assert verdicts[10] == "J4"  # 交付正文（**运行方式：** 小标题）
    assert verdicts[11] == "J1"  # 一次性收尾（"请确认"在 J1 的词表里）
    assert verdicts[12] == "收"  # 真事实不许被误杀（过宽判据是另一种回归）
    assert sorted(dup.item.line for dup in report.duplicates) == [5, 8]
    assert [item.line for item in report.promoted] == [12]
    assert sorted(item.line for item in report.downgraded) == [4, 6, 7, 9, 10, 11]
    assert len(WEATHER) > 400  # 语料前提：天气表真的超长（与真实工作区同一形状）


def test_真事实正例不被误杀(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path,
        lines=["## 项目事实", f"- {FACT}", "", "## 关键决策", "- 用 uv 管理依赖"],
    )
    assert main(["memory", "migrate", "--root", str(root)]) == 0
    report = memory_cmd.analyze(root)

    assert [item.text for item in report.promoted] == [FACT, "用 uv 管理依赖"]
    assert report.rejected == [] and report.duplicates == []


# ---- 验收 2：`--yes` 先备份后写，备份逐字节等于原文 ---------------------------


def test_yes_先备份后写且备份逐字节等于原文(tmp_path: Path, capsys) -> None:
    root = _workspace(tmp_path)
    hot = root / ".agent" / "memory.md"
    before = _bytes(hot)

    assert main(["memory", "migrate", "--yes", "--root", str(root)]) == 0

    backups = sorted((root / ".agent").glob("memory.md.bak-*"))
    assert len(backups) == 1
    assert _bytes(backups[0]) == before  # 逐字节相同
    assert _sha(backups[0]) == hashlib.sha256(before).hexdigest()
    # 新文件只剩"收进热记忆"的那一条真事实。
    assert read_hot(hot) == {"fact": [FACT]}
    out = capsys.readouterr().out
    assert "原文件已保留为" in out and backups[0].name in out


def test_yes_之后库与目录清单都没有别的变化(tmp_path: Path) -> None:
    """`--yes` 只动 `memory.md` 一个文件：不建库、不建表、不写行。"""
    from datetime import date

    root = _workspace(tmp_path)

    assert main(["memory", "migrate", "--yes", "--root", str(root)]) == 0

    assert not (root / ".agent" / "memory.db").exists()
    assert sorted(item.name for item in (root / ".agent").iterdir()) == sorted(
        ["memory.md", f"memory.md.bak-{date.today().isoformat()}"]
    )


def test_重名备份加序号且不覆盖既有备份(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    first = root / ".agent" / "memory.md.bak-2026-10-02"
    first.write_text("上一轮的原文\n", encoding="utf-8")

    from datetime import date

    made = memory_cmd.backup_path(root / ".agent" / "memory.md", today="2026-10-02")
    assert made.name == "memory.md.bak-2026-10-02-2"
    assert first.read_text(encoding="utf-8") == "上一轮的原文\n"
    assert date.today().isoformat()  # 默认日期口径就是今天（报告要写它）


def test_连跑两次yes_第二次的备份是第一次的新内容(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    hot = root / ".agent" / "memory.md"

    assert main(["memory", "migrate", "--yes", "--root", str(root)]) == 0
    after_first = _bytes(hot)
    assert main(["memory", "migrate", "--yes", "--root", str(root)]) == 0

    backups = sorted((root / ".agent").glob("memory.md.bak-*"))
    assert len(backups) == 2  # 两个备份都在，第一个没被覆盖
    assert _bytes(backups[0]) != _bytes(backups[1])
    assert after_first in (_bytes(backups[0]), _bytes(backups[1]))
    assert read_hot(hot) == {"fact": [FACT]}  # 幂等：内容不变


# ---- 验收 3：边界 ------------------------------------------------------------


def test_空工作区不建文件(tmp_path: Path, capsys) -> None:
    root = tmp_path / "empty"
    (root / ".agent").mkdir(parents=True)

    assert main(["memory", "migrate", "--root", str(root)]) == 0

    out = capsys.readouterr().out
    assert "不存在：无可迁移" in out
    assert sorted(item.name for item in (root / ".agent").iterdir()) == []


def test_只有标题的memory_点md_报无可迁移(tmp_path: Path, capsys) -> None:
    root = tmp_path / "titled"
    (root / ".agent").mkdir(parents=True)
    (root / ".agent" / "memory.md").write_text(
        "# 项目记忆（自动维护，也可手动编辑）\n", encoding="utf-8"
    )
    assert main(["memory", "migrate", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "解析：0 条" in out and "无可迁移" in out


def test_dry_run与yes_互斥(tmp_path: Path) -> None:
    """互斥由 argparse 保证：给了两个就退出（`SystemExit(2)`），不是先写后问。"""
    root = _workspace(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        main(["memory", "migrate", "--dry-run", "--yes", "--root", str(root)])
    assert excinfo.value.code == 2
    # 文件一个字节没动：条目数还是 9，没有备份。
    assert sum(len(v) for v in read_hot(root / ".agent" / "memory.md").values()) == 9
    assert not list((root / ".agent").glob("*.bak-*"))


# ---- 验收 4：端到端**不读** `.env` -------------------------------------------
#
# 为什么不用"monkeypatch 密钥解析路径"当唯一证据：`app.py` 里
# `hasattr(args, "provider")` 确实会让"缺供应商向导 + describe_missing + 密钥解析"
# 整段短路，但那是**间接**证据——有人给 `memory` 子命令加一个 `--provider`
# 参数、或在命令体内直接调 `settings.resolve`，这个用例照样绿。所以分两层：
#
# 1. **进程内间谍**：patch `envfile.load_env_file`（读 `.env` 的**唯一**入口）
#    与 `settings.describe_missing`，断言一次都没被调用——这是行为证据；
# 2. **真子进程**：工作区放一个含哨兵的 `.env`，让 `spool` 从真实命令行跑，
#    断言退出码 0、报告正常、且 stdout 里没有哨兵——这是端到端证据
#    （真实调用路径里没有 monkeypatch 帮忙）。


def test_migrate_不解析工作区env(tmp_path: Path, monkeypatch, capsys) -> None:
    root = _workspace(tmp_path)
    sentinel = "SPOOLKIT_FAKE_SECRET=哨兵-不该被读到\n"
    (root / ".env").write_text(sentinel, encoding="utf-8")

    calls: list[str] = []
    import spoolkit.envfile as envfile
    import spoolkit.settings as settings

    real_load = envfile.load_env_file

    def spy_load(path):
        calls.append(f"load_env_file:{path}")
        return real_load(path)

    monkeypatch.setattr(envfile, "load_env_file", spy_load)
    monkeypatch.setattr(settings, "describe_missing", lambda *a, **k: calls.append("describe_missing") or "")
    monkeypatch.setattr(
        settings, "resolve_secret", lambda *a, **k: calls.append("resolve_secret") or ("", "")
    )

    assert main(["memory", "migrate", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "解析：9 条" in out

    assert calls == []
    # 报告里也不该出现哨兵。
    assert "哨兵" not in out


def test_migrate_真子进程不读工作区env(tmp_path: Path, monkeypatch) -> None:
    root = _workspace(tmp_path)
    (root / ".env").write_text("SPOOLKIT_FAKE_SECRET=哨兵-WS-9f2c\n", encoding="utf-8")
    user_env = tmp_path / "user" / ".env"
    user_env.parent.mkdir()
    user_env.write_text("SPOOLKIT_FAKE_SECRET=哨兵-USER-9f2c\n", encoding="utf-8")

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(Path(__file__).resolve().parents[2] / "src"), str(Path(__file__).resolve().parents[2] / "scripts")]
    )
    env["PYTHONIOENCODING"] = "utf-8"
    env["SPOOLKIT_ENV_FILE"] = str(user_env)
    # 后门检测：只要走了 `resolve_provider_args`，`provider_source` 就会变成"环境变量"，
    # 而它进的是 `.env` 密钥那一层同一段装配路径。哨兵值本身不会出现在输出里，
    # 所以这里断言的是"这段路径没被走到"的旁证 + 没有密钥泄漏。
    env["SPOOLKIT_PROVIDER"] = "llamacpp"

    done = subprocess.run(
        [sys.executable, "-m", "spoolkit.cli.app", "memory", "migrate", "--root", str(root)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=120,
    )

    assert done.returncode == 0, done.stderr
    assert "memory.md 迁移预演" in done.stdout
    assert "解析：9 条" in done.stdout
    assert "哨兵" not in done.stdout and "哨兵" not in done.stderr
    assert "SPOOLKIT_FAKE_SECRET" not in done.stdout
    # dry-run 的副作用面：那个工作区的文件一个字节没变、目录无新增。
    assert sorted(item.name for item in (root / ".agent").iterdir()) == ["memory.md"]
    assert "memory.db" not in [item.name for item in (root / ".agent").iterdir()]
