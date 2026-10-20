"""读、搜、跑、验的成功回执拆成信封。content 仍是原来的正文。"""

import sys
from pathlib import Path

from spoolkit.tools.exec import run_command_spec
from spoolkit.tools.fs import read_file_spec
from spoolkit.tools.search import search_code_spec
from spoolkit.tools.verify import verify_file_spec


def test_read_file成功带信封且正文仍在content(tmp_path: Path) -> None:
    body = "".join(f"line {index}\n" for index in range(10))
    (tmp_path / "small.py").write_text(body, encoding="utf-8")
    result = read_file_spec(tmp_path).handler({"path": "small.py"})
    assert result.ok is True
    assert result.content == body
    assert result.raw == body
    assert "small.py" in result.summary
    assert "10 行" in result.summary
    assert "line 0" not in result.summary
    assert result.data["path"] == "small.py"
    assert result.data["start"] == 1
    assert result.data["end"] == 10
    assert result.data["lines"] == 10
    assert result.data["fingerprint"].endswith("/10行")
    assert result.artifacts[0].type == "file"
    assert result.artifacts[0].path == "small.py"


def test_read_file区间也带行号片段(tmp_path: Path) -> None:
    text = "".join(f"line {index}\n" for index in range(20))
    (tmp_path / "small.py").write_text(text, encoding="utf-8")
    result = read_file_spec(tmp_path).handler(
        {"path": "small.py", "start_line": 4, "end_line": 6}
    )
    assert "4\tline 3" in result.content
    assert result.raw == result.content
    assert "第 4–6 行" in result.summary
    assert result.data["start"] == 4
    assert result.data["end"] == 6
    assert result.data["lines"] == 20
    assert "line 3" not in result.summary


def test_read_file失败不套信封(tmp_path: Path) -> None:
    result = read_file_spec(tmp_path).handler({"path": "missing.py"})
    assert result.ok is False
    assert result.summary == ""
    assert result.raw == ""
    assert "不存在" in result.content


def test_search_summary带前几条命中且raw是完整结果(tmp_path: Path) -> None:
    names = [f"f{index}.py" for index in range(8)]
    for name in names:
        (tmp_path / name).write_text(f"HIT_{name}_TOKEN\n", encoding="utf-8")
    result = search_code_spec(tmp_path).handler({"pattern": "HIT_"})
    hits = result.data["hits"]
    assert result.ok is True
    assert len(hits) == 8
    assert result.raw == result.content
    assert all(hit["text"] in result.raw for hit in hits)
    assert "命中 8 条" in result.summary
    covered = [hit for hit in hits if hit["text"] in result.summary]
    assert len(covered) >= 5 or "摘要已截断" in result.summary
    assert len(covered) < len(hits)
    assert result.data["count"] == 8
    assert result.artifacts
    assert {item.type for item in result.artifacts} == {"file"}


def test_search优先不同文件并写明条数截断(tmp_path: Path) -> None:
    for name in ("a.py", "b.py"):
        text = "\n".join(f"MARK_{name}_{index}" for index in range(4)) + "\n"
        (tmp_path / name).write_text(text, encoding="utf-8")
    wide = search_code_spec(tmp_path).handler({"pattern": "MARK_"})
    assert wide.data["count"] == 8
    assert "a.py" in wide.summary and "b.py" in wide.summary
    assert "命中 8 条" in wide.summary
    covered = [hit for hit in wide.data["hits"] if f"{hit['path']}:{hit['line']}:" in wide.summary]
    assert len(covered) >= 5 or "摘要已截断" in wide.summary
    assert all(hit["text"] in wide.raw for hit in wide.data["hits"])

    capped = search_code_spec(tmp_path).handler({"pattern": "MARK_", "max_results": 3})
    assert capped.data["truncated"] is True
    assert "已按 max_results 截断" in capped.summary
    assert all(hit["text"] in capped.raw for hit in capped.data["hits"])


def test_search摘要超长时写明截断() -> None:
    from spoolkit.tools.search import _envelope_search

    path = "p" * 180 + ".py"
    lines = [f"{path}:{index}:TOKEN_{index}" for index in range(1, 6)]
    result = _envelope_search("\n".join(lines), lines, truncated=False)
    assert len(result.summary) <= 240
    assert "摘要已截断" in result.summary
    assert "命中 5 条" in result.summary
    assert result.raw.count("TOKEN_") == 5


def test_search失败不套信封(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    result = search_code_spec(tmp_path).handler({"pattern": "("})
    assert result.ok is False
    assert result.summary == ""
    assert "搜索失败" in result.content


def test_run_summary有退出码和错误行且raw有整段输出(tmp_path: Path) -> None:
    script = tmp_path / "boom.py"
    script.write_text(
        "import sys\n"
        "print('NOISE_HEAD')\n"
        "print('ERROR_KEY_LINE boom', file=sys.stderr)\n"
        "print('RAW_TAIL_ONLY')\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    result = run_command_spec(tmp_path).handler(
        {"command": [sys.executable, str(script)]}
    )
    assert result.ok is False
    assert "退出码 1" in result.content
    assert "RAW_TAIL_ONLY" in result.content
    assert "退出码 1" in result.summary
    assert "ERROR_KEY_LINE boom" in result.summary
    assert len(result.summary) <= 240
    assert "NOISE_HEAD" in result.raw
    assert "ERROR_KEY_LINE boom" in result.raw
    assert "RAW_TAIL_ONLY" in result.raw
    assert result.data["exit"] == 1
    assert result.data["timed_out"] is False
    assert result.data["cwd"] == "."
    assert result.data["argv"][0] == sys.executable
    assert result.artifacts[0].type == "command"


def test_失败命令摘要留首错和末行且不超过240字(tmp_path: Path) -> None:
    lines = ["ERROR_KEY_LINE boom"]
    lines += [f"noise {index}" for index in range(1, 99)]
    lines.append("LAST_CONTEXT_LINE")
    assert len(lines) == 100
    script = tmp_path / "long.py"
    script.write_text(
        "import sys\n"
        + "".join(f"print({line!r}, file=sys.stderr)\n" for line in lines)
        + "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    result = run_command_spec(tmp_path).handler(
        {"command": [sys.executable, str(script)]}
    )
    assert result.ok is False
    assert len(result.summary) <= 240
    assert "退出码 1" in result.summary
    assert "ERROR_KEY_LINE boom" in result.summary
    assert "LAST_CONTEXT_LINE" in result.summary
    assert "仅保留关键片段" in result.summary
    assert "noise 50" not in result.summary
    assert "noise 50" in result.raw
    assert "LAST_CONTEXT_LINE" in result.raw
    assert result.data["exit"] == 1
    assert result.data["timed_out"] is False
    assert result.artifacts[0].type == "command"


def test_成功时summary不附带末尾输出(tmp_path: Path) -> None:
    script = tmp_path / "ok_out.py"
    script.write_text(
        "print('FIRST_ONLY')\n"
        + "".join(f"print('pad {index}')\n" for index in range(30))
        + "print('SUCCESS_TAIL')\n",
        encoding="utf-8",
    )
    result = run_command_spec(tmp_path).handler(
        {"command": [sys.executable, str(script)]}
    )
    assert result.ok is True
    assert result.summary.startswith("退出码 0")
    assert "SUCCESS_TAIL" not in result.summary
    assert "SUCCESS_TAIL" in result.raw
    assert "仅保留关键片段" not in result.summary


def test_run拒绝不套信封(tmp_path: Path) -> None:
    result = run_command_spec(tmp_path).handler({"command": ["shutdown", "/s"]})
    assert result.ok is False
    assert result.summary == ""
    assert "永久禁止" in result.content


def test_verify一行结论且问题和备注在data(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text("x = 1\n", encoding="utf-8")
    clean = verify_file_spec(tmp_path).handler({"path": "ok.py"})
    assert clean.ok is True
    assert clean.raw == ""
    assert "没有发现问题" in clean.summary
    assert "\n" not in clean.summary
    assert "Python 语法编译：通过" in clean.content
    assert "Python 语法编译：通过" not in clean.summary
    assert clean.data["path"] == "ok.py"
    assert clean.data["suffix"] == ".py"
    assert clean.data["problems"] == []
    assert any("通过" in note for note in clean.data["notes"])
    assert clean.artifacts[0].type == "file"
    assert clean.artifacts[0].path == "ok.py"

    (tmp_path / "bad.py").write_text("def (\n", encoding="utf-8")
    broken = verify_file_spec(tmp_path).handler({"path": "bad.py"})
    assert broken.ok is False
    assert broken.data["problems"]
    assert broken.data["problems"][0] in broken.summary
    assert "按「文件:行」逐条改。" in broken.content
    assert "按「文件:行」逐条改。" not in broken.summary
