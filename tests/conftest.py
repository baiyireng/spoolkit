"""测试的公共隔离。

一条硬规矩：**测试绝不读开发机上的用户配置**。用户级配置（`spool config`）
是给这台机器的默认值——它一旦泄漏进测试，"在我这儿能过"就取决于某个人配了
什么，而这正是最不该出现在测试里的一种依赖（真实事故见下）。

事故：`test_脚本文件不存在时返回非零` 原本不带 `--provider fake`，靠"没配过
任何供应商 → 默认假模型"才走到那条分支；开发机上配了 llamacpp 之后，
这条测试变成真的去连本地服务，于是断言失败、而且失败原因看起来像代码坏了。
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_user_config(tmp_path_factory, monkeypatch):
    path = tmp_path_factory.mktemp("user-config") / "config.toml"
    monkeypatch.setenv("SPOOLKIT_CONFIG", str(path))
    # 旧名字也要堵住：改名兼容让我们**继续读** `AGENTS_DEV_*`，
    # 若它从开发机泄漏进来，测试就会变成"在我这儿能过"。
    monkeypatch.delenv("AGENTS_DEV_CONFIG", raising=False)
    for name in ("PROVIDER", "MODEL", "BASE_URL", "PROXY", "SCRIPT"):
        monkeypatch.delenv(f"AGENTS_DEV_{name}", raising=False)
        monkeypatch.delenv(f"SPOOLKIT_{name}", raising=False)
    yield
