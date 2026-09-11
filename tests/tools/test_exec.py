import sys
from pathlib import Path

from agents_dev.tools.exec import (
    MAX_OUTPUT_CHARS,
    resolve_argv,
    run_command_spec,
    validate_command,
)


def _run(tmp_path: Path, **kwargs):
    return run_command_spec(tmp_path).handler(kwargs)


def _script(tmp_path: Path, name: str, body: str) -> str:
    (tmp_path / name).write_text(body, encoding="utf-8")
    return str(tmp_path / name)


def test_白名单外的命令被拒绝(tmp_path: Path) -> None:
    result = _run(tmp_path, command=["rm", "-rf", "/"])
    assert result.ok is False
    assert "不在白名单内" in result.content


def test_python任意代码执行入口被拒绝(tmp_path: Path) -> None:
    assert validate_command(["python", "-c", "print(1)"]) is not None
    assert validate_command(["python", "-i"]) is not None


def test_python模块形式被允许() -> None:
    assert validate_command(["python", "-m", "pytest", "-q"]) is None


def test_运行项目内脚本(tmp_path: Path) -> None:
    script = _script(tmp_path, "hello.py", "print('来自脚本的输出')\n")
    result = _run(tmp_path, command=[sys.executable, script])
    assert result.ok is True
    assert "来自脚本的输出" in result.content
    assert "退出码 0" in result.content


def test_脚本报错时返回失败与输出(tmp_path: Path) -> None:
    script = _script(tmp_path, "boom.py", "raise SystemExit(3)\n")
    result = _run(tmp_path, command=[sys.executable, script])
    assert result.ok is False
    assert "退出码 3" in result.content


def test_参数里的shell元字符不被解释(tmp_path: Path) -> None:
    script = _script(tmp_path, "echo.py", "import sys\nprint(sys.argv[1:])\n")
    result = _run(tmp_path, command=[sys.executable, script, "a; whoami"])
    assert result.ok is True
    assert "a; whoami" in result.content


def test_只读git子命令被允许() -> None:
    assert validate_command(["git", "status"]) is None
    assert validate_command(["git", "diff"]) is None


def test_会改写状态的git子命令被拒绝() -> None:
    for sub in ("commit", "reset", "checkout", "clean", "push"):
        assert validate_command(["git", sub]) is not None


def test_超时被终止(tmp_path: Path) -> None:
    script = _script(tmp_path, "slow.py", "import time\ntime.sleep(10)\n")
    result = _run(tmp_path, command=[sys.executable, script], timeout=1)
    assert result.ok is False
    assert "未结束" in result.content


def test_超时参数被校验(tmp_path: Path) -> None:
    script = _script(tmp_path, "ok.py", "print(1)\n")
    assert _run(tmp_path, command=[sys.executable, script], timeout=0).ok is False


def test_工作目录越界被拒绝(tmp_path: Path) -> None:
    script = _script(tmp_path, "ok.py", "print(1)\n")
    result = _run(tmp_path, command=[sys.executable, script], cwd="../..")
    assert result.ok is False


def test_工作目录不存在时失败(tmp_path: Path) -> None:
    script = _script(tmp_path, "ok.py", "print(1)\n")
    result = _run(tmp_path, command=[sys.executable, script], cwd="nope")
    assert result.ok is False


def test_超长输出被截断并标注(tmp_path: Path) -> None:
    script = _script(tmp_path, "big.py", "print('x' * 20000)\n")
    result = _run(tmp_path, command=[sys.executable, script])
    assert result.ok is True
    assert "输出过长" in result.content
    assert len(result.content) < MAX_OUTPUT_CHARS + 500


def test_可以跑真实的pytest(tmp_path: Path) -> None:
    (tmp_path / "test_sample.py").write_text(
        "def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8"
    )
    result = _run(
        tmp_path,
        command=[sys.executable, "-m", "pytest", "-q"],
        timeout=120,
    )
    assert result.ok is True
    assert "1 passed" in result.content


def test_失败测试会返回非零并带上输出(tmp_path: Path) -> None:
    (tmp_path / "test_bad.py").write_text(
        "def test_bad():\n    assert False\n", encoding="utf-8"
    )
    result = _run(
        tmp_path, command=[sys.executable, "-m", "pytest", "-q"], timeout=120
    )
    assert result.ok is False
    assert "test_bad" in result.content


def test_裸python被换算成可用解释器(tmp_path: Path) -> None:
    resolved = resolve_argv(["python", "-m", "pytest", "-q"], tmp_path)
    assert resolved[0] != "python"
    assert Path(resolved[0]).exists()
    assert resolved[1:] == ["-m", "pytest", "-q"]


def test_裸pytest被换算成模块调用(tmp_path: Path) -> None:
    resolved = resolve_argv(["pytest", "-q"], tmp_path)
    assert resolved[1:] == ["-m", "pytest", "-q"]


def test_python3也走同样的换算(tmp_path: Path) -> None:
    assert resolve_argv(["python3", "x.py"], tmp_path)[1:] == ["x.py"]


def test_优先使用项目虚拟环境(tmp_path: Path) -> None:
    venv_dir = tmp_path / ".venv" / "Scripts"
    venv_dir.mkdir(parents=True)
    fake = venv_dir / "python.exe"
    fake.write_text("", encoding="utf-8")
    assert resolve_argv(["python"], tmp_path)[0] == str(fake)


def test_非python命令不做换算(tmp_path: Path) -> None:
    assert resolve_argv(["git", "status"], tmp_path) == ["git", "status"]


def test_裸python写法的测试能跑通(tmp_path: Path) -> None:
    (tmp_path / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    result = _run(tmp_path, command=["python", "-m", "pytest", "-q"], timeout=120)
    assert result.ok is True
    assert "1 passed" in result.content


def test_裸pytest写法也能跑通(tmp_path: Path) -> None:
    (tmp_path / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    result = _run(tmp_path, command=["pytest", "-q"], timeout=120)
    assert result.ok is True


def test_回显的是模型写的命令而不是解释器路径(tmp_path: Path) -> None:
    script = _script(tmp_path, "ok.py", "print(1)\n")
    result = _run(tmp_path, command=["python", script])
    assert "$ python " in result.content
    assert ".venv" not in result.content.splitlines()[1]
