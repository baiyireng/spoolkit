from spoolkit.tools.exec import validate_command


def test_允许uv_add合法包名() -> None:
    assert validate_command(["uv", "add", "httpx"]) is None
    assert validate_command(["uv", "add", "httpx", "pytest"]) is None


def test_允许带版本约束与extras() -> None:
    assert validate_command(["uv", "add", "httpx>=0.27"]) is None
    assert validate_command(["uv", "add", "httpx[socks]==0.28.1"]) is None


def test_允许uv_sync与lock() -> None:
    assert validate_command(["uv", "sync"]) is None
    assert validate_command(["uv", "sync", "--extra", "dev"]) is None
    assert validate_command(["uv", "lock"]) is None


def test_允许uv_remove() -> None:
    assert validate_command(["uv", "remove", "httpx"]) is None


def test_拒绝直接安装本地路径() -> None:
    assert validate_command(["uv", "add", "./local_pkg"]) is not None
    assert validate_command(["uv", "add", "../evil"]) is not None


def test_拒绝URL安装() -> None:
    assert validate_command(["uv", "add", "https://x.com/a.zip"]) is not None
    assert validate_command(["uv", "add", "git+https://x.com/a.git"]) is not None


def test_拒绝会绕过校验的参数() -> None:
    for flag in ("--index-url", "--extra-index-url", "--find-links", "--url"):
        assert validate_command(["uv", "add", flag, "http://x"]) is not None
    assert validate_command(["uv", "add", "-e", "."]) is not None
    assert validate_command(["uv", "add", "-r", "req.txt"]) is not None


def test_拒绝pip() -> None:
    assert validate_command(["pip", "install", "httpx"]) is not None
    assert validate_command(["pip3", "install", "httpx"]) is not None


def test_拒绝非白名单的uv子命令() -> None:
    assert validate_command(["uv", "python", "install"]) is not None
    assert validate_command(["uv", "pip", "install", "httpx"]) is not None


def test_uv_run仍走原有白名单() -> None:
    assert validate_command(["uv", "run", "pytest", "-q"]) is None
    assert validate_command(["uv", "run", "python", "-c", "print(1)"]) is not None


def test_拒绝注入式包名() -> None:
    assert validate_command(["uv", "add", "httpx; rm -rf /"]) is not None
    assert validate_command(["uv", "add", "httpx | whoami"]) is not None

