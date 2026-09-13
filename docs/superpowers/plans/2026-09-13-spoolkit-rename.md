# spoolkit 改名与任意位置起手 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `spool` 更名为 `spoolkit`（命令 `spool`），并让任意目录都能起手与批准配对，同时补齐 README 与使用手册。

**Architecture:** 命名改动是机械替换 + 一层**兼容读**（旧命令名、旧环境变量、旧配置路径都继续认）；行为改动集中在两处：工作区解析统一收到 `main()` 里（`--root` 默认 `None`，按"显式 → 向上找 `.agent/` → 全局登记表 → 当前目录"解析），以及无参数启动时的初始化询问。

**Tech Stack:** Python 3.12+，无新增运行时依赖；pytest；hatchling；argparse。

## Global Constraints

- 发行名 `spoolkit`，控制台命令 `spool`，另留 `spoolkit` 与 `spool` 两个别名脚本
- 模块名 `spoolkit`（原 `spoolkit`），源码布局 `src/`
- **用户机器上已有的数据一律不动**：`.agent/` 目录名不改；旧环境变量 `AGENTS_DEV_*` 继续认；旧配置 `%APPDATA%\spool\config.toml` 与 `~/.spool/diagnosis.key` 继续读
- 新环境变量前缀 `SPOOLKIT_`，新配置目录 `%APPDATA%\spoolkit`（其它平台 `~/.config/spoolkit`）
- 注释与文档用中文，标识符用英文
- MIT，作者 Aaron <aaron_gb2022@outlook.com>，仓库 `github.com/baiyireng/spoolkit`
- 每个任务结束都必须跑通相关测试；最后跑全套 + 3.12 干净环境

---

### Task 1: 机械改名（包、模块、命令、元数据）

**Files:**
- Modify: `src/spoolkit/**` → `src/spoolkit/**`（目录改名 + 全部 import）
- Modify: `tests/**`（全部 import）
- Modify: `pyproject.toml`、`README.md`、`docs/*.md`
- Create: 无（LICENSE 已存在）

**Interfaces:**
- Produces: 模块路径 `spoolkit.*`；控制台脚本 `spool` / `spoolkit` / `spool`；显示名 `spool`（`argparse` 的 `prog`）

- [ ] **Step 1: 改名目录**

```powershell
git mv src/spoolkit src/spoolkit
```

- [ ] **Step 2: 全仓文本替换**

替换规则（顺序不能反）：`spoolkit` → `spoolkit`；`spool` → `spool`；
**但** `AGENTS_DEV_` 前缀单独留给 Task 2 处理（它是环境变量，要新旧并存）。

```powershell
Get-ChildItem src,tests,docs -Recurse -File -Include *.py,*.md,*.toml,*.json |
  Where-Object { $_.FullName -notmatch '__pycache__' } |
  ForEach-Object { (Get-Content -Raw -Encoding UTF8 $_).Replace('spoolkit','spoolkit').Replace('spool','spool') |
    Set-Content -NoNewline -Encoding UTF8 $_ }
```

- [ ] **Step 3: 元数据**

`pyproject.toml`：`name = "spoolkit"`；三个脚本入口；`[project.urls]`；hatch 的
`packages = ["src/spoolkit"]`。

- [ ] **Step 4: 重新安装开发版并跑测试**

```powershell
uv pip install -e ".[dev]" ; pytest -q
```

期望：全部通过（此时仍是 1133 条）。这条**必须真跑**：改了入口点不重装，
`spool` 这个脚本不会存在。

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "refactor: 更名 spoolkit（包/模块/命令），保留 spool 别名"
```

### Task 2: 兼容层（环境变量、配置路径、诊断密钥）

**Files:**
- Modify: `src/spoolkit/settings.py`（`ENV_PREFIX`、`config_path()`、写入时迁移）
- Modify: `src/spoolkit/bridge/credentials.py`、`src/spoolkit/bridge/plugins.py`
- Modify: `src/spoolkit/diagnosis.py`（密钥路径）
- Test: `tests/test_settings_compat.py`、`tests/test_diagnosis_key_compat.py`

**Interfaces:**
- Produces: `settings.ENV_PREFIX = "SPOOLKIT_"`；`settings.LEGACY_ENV_PREFIX = "AGENTS_DEV_"`；
  `settings.config_path()` 返回新路径，新路径不存在而旧文件在时 `read_config()` 仍读旧文件

- [ ] **Step 1: 先写失败测试**

```python
def test_旧环境变量仍然生效(monkeypatch):
    monkeypatch.delenv("SPOOLKIT_PROVIDER", raising=False)
    monkeypatch.setenv("AGENTS_DEV_PROVIDER", "llamacpp")
    assert settings.load()["provider"] == "llamacpp"

def test_新环境变量优先(monkeypatch):
    monkeypatch.setenv("AGENTS_DEV_PROVIDER", "llamacpp")
    monkeypatch.setenv("SPOOLKIT_PROVIDER", "gemini")
    assert settings.load()["provider"] == "gemini"
```

- [ ] **Step 2: 跑测试确认失败**

`pytest tests/test_settings_compat.py -v` → 期望失败（旧变量没被读）

- [ ] **Step 3: 实现兼容读**

取值顺序：`SPOOLKIT_<KEY>` → `AGENTS_DEV_<KEY>` → 用户配置。
配置路径：新路径没有文件而旧路径有 → 读旧的；一旦要写，写到新路径。

- [ ] **Step 4: 跑测试确认通过，并提交**

### Task 3: 工作区解析与全局登记表

**Files:**
- Create: `src/spoolkit/workspace.py`（`find_root` / `register` / `known` / `state_dir`）
- Modify: `src/spoolkit/cli/app.py`（`--root` 默认 `None`，`main()` 里统一解析）
- Modify: `src/spoolkit/onboarding.py`（初始化时登记）
- Test: `tests/test_workspace_root.py`

**Interfaces:**
- Produces: `workspace.resolve_root(explicit: str | None, *, choose=...) -> Path`；
  `workspace.register(root: Path) -> None`；`workspace.known() -> list[Path]`

- [ ] **Step 1: 先写失败测试**

```python
def test_子目录里能找到工作区根(tmp_path, monkeypatch):
    (tmp_path / ".agent").mkdir()
    deep = tmp_path / "src" / "pkg"; deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    assert workspace.resolve_root(None) == tmp_path

def test_登记表能在别处定位工作区(tmp_path, monkeypatch):
    monkeypatch.setenv("SPOOLKIT_STATE", str(tmp_path / "state"))
    (tmp_path / "ws" / ".agent").mkdir(parents=True)
    workspace.register(tmp_path / "ws")
    monkeypatch.chdir(tmp_path)          # 不是工作区
    assert workspace.resolve_root(None) == (tmp_path / "ws").resolve()
```

- [ ] **Step 2: 跑测试确认失败** → ImportError
- [ ] **Step 3: 实现 `workspace.py`**（规则见设计档"任意位置起手"一节）
- [ ] **Step 4: 跑测试确认通过**
- [ ] **Step 5: 在 `main()` 里统一解析**，跑全套确认没有回归（这一步最容易碰坏别的命令）
- [ ] **Step 6: Commit**

### Task 4: `spool approve` 与无参数启动

**Files:**
- Modify: `src/spoolkit/cli/app.py`（新增 `approve` 子命令；无参数走初始化询问）
- Modify: `src/spoolkit/cli/commands/bridge.py`（`--approve` 与顶层复用同一函数）
- Test: `tests/cli/test_approve_command.py`

**Interfaces:**
- Consumes: `workspace.resolve_root`
- Produces: `approve_command(args) -> int`（在 `cli/commands/bridge.py`）

- [ ] **Step 1: 先写失败测试**：在临时工作区造一个配对文件 → 从**别的目录**调
  `main(["approve", "<码>"])` → 断言配对文件里出现了这个人
- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现**（`approve` 复用 `approve_command`；无参数时若不在工作区则问
  一句"在这里初始化吗"，`y` 就跑 init 再进对话）
- [ ] **Step 4: 跑测试确认通过 + 全套**
- [ ] **Step 5: Commit**

### Task 5: 文档

**Files:**
- Modify: `README.md`（重写：定位、30 秒上手、命令表）
- Create: `docs/getting-started.md`、`docs/cli.md`、`docs/manual.md`
- Modify: `docs/bridge.md`、`docs/mcp.md`、`docs/benchmarking.md`、`docs/dogfooding.md`（命令名与交叉引用）

- [ ] **Step 1: 写 README**（第一屏 = 一句话定位 + 30 秒上手 + 命令表）
- [ ] **Step 2: 写 `docs/getting-started.md`**（安装、首次向导、任意目录初始化、供应商与代理、常见故障）
- [ ] **Step 3: 写 `docs/cli.md`**（逐条命令与参数，从 `--help` 抄准）
- [ ] **Step 4: 写 `docs/manual.md`**（工作区／三档权限／记忆与归档／长任务与子智能体／通道接入／排错）
- [ ] **Step 5: 更新既有 docs 的命令名与引用**
- [ ] **Step 6: Commit**

### Task 6: 验证与发布

**Files:**
- Modify: `.gitignore`（如需）、`pyproject.toml`（`[project.urls]` 兜底检查）

- [ ] **Step 1: 全套测试**（主环境 + 3.12 干净环境）
- [ ] **Step 2: 真机各走一次**：`spool --version`、`spool init`（临时目录）、
  `spool approve`（从别的目录）、`spool chat` 一句话
- [ ] **Step 3: `git remote add origin git@github.com:baiyireng/spoolkit.git`**
- [ ] **Step 4: 推送**（需要用户装好 `gh` 或提供 PAT）
