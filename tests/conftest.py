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
