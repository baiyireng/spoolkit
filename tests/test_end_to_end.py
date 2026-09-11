import json
from pathlib import Path

from agents_dev.cli.app import build_loop, main


def test_端到端完成一次读文件并回答(tmp_path: Path) -> None:
    (tmp_path / "config.py").write_text("TIMEOUT = 30\n", encoding="utf-8")
    script = [
        json.dumps(
            {
                "thought": "读取配置",
                "tool_calls": [{"name": "read_file", "arguments": {"path": "config.py"}}],
                "state": {"current": "读 config.py"},
                "done": False,
                "final": None,
            },
            ensure_ascii=False,
        ),
        json.dumps(
            {
                "thought": "已获得答案",
                "tool_calls": [],
                "state": {"done_added": ["读 config.py"]},
                "done": True,
                "final": "超时配置是 30",
            },
            ensure_ascii=False,
        ),
    ]
    result = build_loop(tmp_path, script=script, window=4096).run("config.py 里的超时是多少")
    assert result.finished is True
    assert "30" in result.final
    assert (tmp_path / ".agent" / "tasks" / "task.json").exists()


def test_命令行读取脚本文件并运行(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("答案42\n", encoding="utf-8")
    script_path = tmp_path / "script.json"
    script_path.write_text(
        json.dumps(
            [
                {
                    "thought": "读文件",
                    "tool_calls": [{"name": "read_file", "arguments": {"path": "a.txt"}}],
                    "state": None,
                    "done": False,
                    "final": None,
                },
                {
                    "thought": "完成",
                    "tool_calls": [],
                    "state": None,
                    "done": True,
                    "final": "答案是 42",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    exit_code = main(
        [
            "run",
            "--goal",
            "a.txt 里是什么",
            "--script",
            str(script_path),
            "--root",
            str(tmp_path),
            "--window",
            "4096",
        ]
    )
    assert exit_code == 0


def test_脚本文件不存在时返回非零(tmp_path: Path) -> None:
    exit_code = main(
        [
            "run",
            "--goal",
            "x",
            "--script",
            str(tmp_path / "nope.json"),
            "--root",
            str(tmp_path),
        ]
    )
    assert exit_code == 2


def test_命令行装配后索引工具已注册且预取生效(tmp_path: Path) -> None:
    (tmp_path / "parser.py").write_text(
        "def parse_config(path):\n    return path\n", encoding="utf-8"
    )
    loop = build_loop(tmp_path, script=[_最简单的一轮()], window=4096)

    assert loop.registry.get("find_symbol") is not None
    assert loop.registry.get("file_symbols") is not None
    assert loop.prefetch is not None
    assert "parser.py" in loop.prefetch("修复 parse_config")


def _最简单的一轮() -> str:
    return json.dumps(
        {"thought": "完成", "tool_calls": [], "state": None, "done": True, "final": "好"},
        ensure_ascii=False,
    )

