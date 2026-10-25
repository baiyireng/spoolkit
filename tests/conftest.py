"""测试的公共隔离。

一条硬规矩：**测试绝不读开发机上的用户配置**。用户级配置（`spool config`）
是给这台机器的默认值——它一旦泄漏进测试，"在我这儿能过"就取决于某个人配了
什么，而这正是最不该出现在测试里的一种依赖（真实事故见下）。

事故：`test_脚本文件不存在时返回非零` 原本不带 `--provider fake`，靠"没配过
任何供应商 → 默认假模型"才走到那条分支；开发机上配了 llamacpp 之后，
这条测试变成真的去连本地服务，于是断言失败、而且失败原因看起来像代码坏了。
"""

import os
import pathlib
import tempfile

import pytest

from spoolkit import sandbox_dirs
from spoolkit.memory.transcript import clear_run_origin


@pytest.fixture(autouse=True)
def _isolate_run_origin():
    """每个用例都从「这个进程还没跑过运行」开始。

    `memory.transcript` 的运行起点是**进程级**的：`spool run` 一次运行一个
    进程，所以那是最自然的粒度（运行开始时标定一次，注入文本里的时间就都
    相对它归一化）。用例之间共用同一个进程，于是**上一个用例里
    `AgentLoop.run()` 标下的起点会漏到下一个用例**——症状是「没跑过运行时
    时间只到日期」这条兜底在单独跑时成立、整套跑时失效（真踩过：
    `test_历史检索::test_时间方向是新到旧` 在只跑那一个文件时绿、全套时红）。

    这是隔离，不是改判据：产品里的起点仍然只在一次运行的范围内有意义。
    """
    clear_run_origin()
    yield
    clear_run_origin()


@pytest.fixture(autouse=True)
def _isolate_cwd():
    """每个用例结束时，**进程的工作目录必须回到它进来时的样子**。

    这是顺序隔离，不是产品判据：用例之间共用一个进程，于是任何一处
    **没还原的 `chdir`** 都会漏给后面所有用例。症状是最难查的那一类——
    「单跑绿、全跑红」，而且红的是**读仓库文件**的那些判据（它们写成
    `Path("benchmarks/tasks")` / `Path("src/spoolkit")` 这种相对当前目录的
    形状时，量的其实是「跑测试时 cwd 在哪」）。

    夹具在**每个用例的 teardown** 检查，所以报出来的就是泄漏的那一条用例
    （`monkeypatch.chdir` 的还原排在本夹具之前，用它不会误报）。
    """
    before = os.getcwd()
    yield
    after = os.getcwd()
    assert after == before, (
        f"这个用例改了进程的工作目录且没有还原：{before} → {after}。"
        "后面所有用例都会在错的目录里跑（「单跑绿、全跑红」的常见成因）。"
        "要用 chdir 就 `monkeypatch.chdir(...)`，它会在 teardown 自动还原。"
    )


@pytest.fixture(autouse=True)
def _isolate_user_config(tmp_path_factory, monkeypatch):
    path = tmp_path_factory.mktemp("user-config") / "config.toml"
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(path))
    # 用户级 `.env`（密钥那一层）同理：开发机上真写过一份的话，测试就会读到
    # 别人的 QQ / Tavily / Gemini 密钥，症状是"在我这儿能过"。
    monkeypatch.setenv(
        "SPOOLKIT_ENV_FILE", str(tmp_path_factory.mktemp("user-env") / ".env")
    )
    monkeypatch.delenv("AGENTS_DEV_ENV_FILE", raising=False)
    # 旧名字也要堵住：改名兼容让我们**继续读** `AGENTS_DEV_*`，
    # 若它从开发机泄漏进来，测试就会变成"在我这儿能过"。
    monkeypatch.delenv("AGENTS_DEV_CONFIG", raising=False)
    # 全局工作区登记表同理：开发机上登记过的工作区一旦泄漏进来，
    # "当前目录不是工作区"这类用例会莫名其妙地指向别人真实的项目。
    monkeypatch.setenv("SPOOLKIT_STATE", str(tmp_path_factory.mktemp("state")))
    for name in ("PROVIDER", "MODEL", "BASE_URL", "PROXY", "SCRIPT"):
        monkeypatch.delenv(f"AGENTS_DEV_{name}", raising=False)
        monkeypatch.delenv(f"SPOOLKIT_{name}", raising=False)
    # 开发机的**代理**同理：`httpx` 默认会读这几个变量，于是"本机起的测试服务器"
    # 走没走代理、行为可能不一样。实测（2026-10-16）：带 `HTTP_PROXY` 时
    # `tests/test_fetch.py::test_超过大小上限就中止` 报 `DID NOT RAISE WebError`，
    # 摘掉代理就绿——那是**这台机器的状态**，不是被测行为。
    for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture
def raw_dir_creation(tmp_path, _isolate_user_config):
    """临时摘掉「把建目录的 mode 抬成 0o777」那层插件补丁，模拟真实产品进程。

    受限环境（Windows + 文件沙箱）里，`pytest_sandbox_compat` 顺手救了**所有**
    0o700 的创建点。但产品代码里的创建点（bench 的任务工作区、试跑副本）
    **不能靠这层补丁活着**——真实 CLI 进程里没有它。所以要钉住这些点的用例
    得先把它摘掉：不摘的话，改回旧写法也照样绿，等于没钉。

    显式依赖 `tmp_path` 与 autouse 的隔离夹具：它们都是用插件**先**建完目录的，
    摘掉之后本用例自己不再需要 pytest 去建目录。
    """
    saved = (pathlib.Path.mkdir, os.mkdir, tempfile.mkdtemp)
    pathlib.Path.mkdir = sandbox_dirs.raw_path_mkdir
    os.mkdir = sandbox_dirs.raw_os_mkdir
    tempfile.mkdtemp = sandbox_dirs.raw_mkdtemp
    try:
        yield
    finally:
        pathlib.Path.mkdir, os.mkdir, tempfile.mkdtemp = saved
