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


def test_永久禁止的命令被拒绝(tmp_path: Path) -> None:
    # 提权与系统级操作才是永久禁止的那一类；删除已经改成可申请了。
    result = _run(tmp_path, command=["shutdown", "/s"])
    assert result.ok is False
    assert "永久禁止" in result.content


def test_删除命令可以申请而不是一律拦死(tmp_path: Path) -> None:
    """一律拦死会留下死路：后续任务确实需要删一个文件时，连问都问不到。

    危险的东西该让人来判，而不是让工具替他判不了。
    """
    result = _run(tmp_path, command=["rm", "-rf", "some_dir"])
    assert result.ok is False
    assert "需要用户批准" in result.content
    assert "用户手动执行" in result.content


def test_白名单外但非禁止的命令需要申请(tmp_path: Path) -> None:
    # 没有询问渠道时应当明确说明，而不是含糊地失败
    result = _run(tmp_path, command=["npm", "run", "build"])
    assert result.ok is False
    assert "需要用户批准" in result.content


def test_python任意代码执行入口被拒绝(tmp_path: Path) -> None:
    assert validate_command(["python", "-c", "print(1)"]) is not None
    assert validate_command(["python", "-i"]) is not None


def test_python模块形式被允许() -> None:
    assert validate_command(["python", "-m", "pytest", "-q"]) is None


def test_整条命令塞进一个元素时说形状而不是权限(tmp_path: Path) -> None:
    """实测踩过：模型把 `python -m pytest a/b.py -q` 当成一个元素传进来。

    白名单于是把它当成一个叫「python -m pytest a/b.py -q」的程序，报
    「不在白名单内，需要用户批准」——**理由指错了方向**。它据此去申请权限，
    无人值守时没人可问，就卡住了（实测烧掉三步，转而自己写脚本，
    最后撞上输出预算收尾）。形状问题就该说形状。
    """
    result = _run(tmp_path, command=["python -m pytest a/b.py -q"])
    assert result.ok is False
    assert "拆成数组" in result.content
    # 不能再说成权限问题：那会让它去申请授权，而这条路走不通
    assert "需要用户批准" not in result.content


def test_后面几个参数带空格是合法的(tmp_path: Path) -> None:
    """pytest 的 `-k "a and b"` 这类参数本来就带空格，不能一起误伤。"""
    result = _run(tmp_path, command=["python", "-m", "pytest", "-k", "a and b"])
    assert "拆成数组" not in result.content


def test_cd这类shell内建要说清正确写法(tmp_path: Path) -> None:
    """实测它连撞十几次「不在白名单内」都不换写法——因为没人告诉它换成什么。

    换目录的正确做法是 cwd 参数；说成「需要用户批准」只会把它引去申请权限。
    """
    for argv in (["cd", "13_x"], ["export", "X=1"]):
        result = _run(tmp_path, command=argv)
        assert result.ok is False
        assert "不用写" in result.content
        assert "cwd" in result.content
        assert "需要用户批准" not in result.content


def test_cd_带命令的写法被翻译成cwd(tmp_path: Path) -> None:
    """`cd X && <命令>` → 「在 X 里执行 <命令>」。翻译，不是放行。

    实测模型会反复用这种 shell 写法，即使报错已经告诉它该用 cwd——它在别处
    学到的习惯比提示词强。一次这样的翻车（连撞三次加重试）能吃掉整轮 27% 的
    token，所以与其继续提醒，不如把它的意思翻对。
    """
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "hello.py").write_text("print('在子目录里跑')\n", encoding="utf-8")
    result = _run(
        tmp_path, command=["cd", "sub", "&&", "python", "hello.py"]
    )
    assert result.ok is True, result.content
    assert "在子目录里跑" in result.content
    # 回显的是模型自己写的那份命令，不是换算后的
    assert "cd sub && python hello.py" in result.content


def test_裸cd仍然要它用cwd(tmp_path: Path) -> None:
    """只写 `cd X` 没有后续命令——那不构成一件事，照旧告诉它怎么写。"""
    result = _run(tmp_path, command=["cd", "sub"])
    assert result.ok is False
    assert "cwd" in result.content


def test_用绝对路径当cwd时说清该写相对路径(tmp_path: Path) -> None:
    result = _run(
        tmp_path, command=["python", "-m", "pytest", "-q"], cwd="/workspace"
    )
    assert result.ok is False
    assert "越出项目根目录" in result.content
    assert "相对于工作区" in result.content


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
