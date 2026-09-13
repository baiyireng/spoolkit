# spoolkit：定名、任意位置起手、文档与发布

日期：2026-09-13　状态：已定，正在实施

## 为什么做这一件事

`spool` 是个描述性占位名，不能当作品的名字：它出现在命令里（每次都要敲
两个词）、出现在 README 标题里、出现在别人 `pip install` 的那一行里。项目要到
能被别人用、能在 GitHub 上被看懂的程度，名字得先定下来。

同时暴露的一个真问题：**配对批准没法在任意位置执行**。`bridge --approve` 读的是
`--root`（默认当前目录）下的 `.agent/bridge-pairings.json`——人在别的目录敲，它会
去翻那个目录的配对文件，然后报"没有这个配对码"。这不是码错，是找错了工作区。

## 命名

- 项目与发行名：**spoolkit**（PyPI `spoolkit` 实测可用；`spool` 已被占用）
- 命令行：**`spool`**。命令名与发行名分开是常规做法（`claude` 命令对应的包是
  `@anthropic-ai/claude-code`），而"手敲的那一下"值得是最短的。
- 为什么是 spool：整套架构的核心动作就是**假脱机**——不把全部塞进上下文，其余
  排队落在磁盘/数据库里，按需取一小段。它是功能描述，不是隐喻。
- 仓库：`github.com/baiyireng/spoolkit`（GitHub 上该名字可用）

## 兼容：改名的同时不许把人现有的东西弄坏

这一节是硬要求。改名是**一次性动作**，但用户机器上已经有状态：

| 旧的东西 | 处理 |
|---|---|
| 命令 `spool` | 保留同名控制台脚本作为别名（至少留一个版本周期），并额外提供 `spoolkit` |
| 环境变量 `AGENTS_DEV_*` | 规范名改成 `SPOOLKIT_*`，**旧名继续认**（与新名并存，新名优先） |
| 用户配置 `%APPDATA%\spool\config.toml` | 新路径 `%APPDATA%\spoolkit\config.toml`；新路径没有而旧文件在时，**继续读旧的**（并在写入时迁移过去） |
| 诊断签名密钥 `~/.spool/diagnosis.key` | 新路径 `~/.spoolkit/diagnosis.key`；旧文件在就读旧的，不重新生成 |
| 工作区状态目录 `.agent/` | **不改名**。里面有记忆、索引、检查点、策略；改名只是好看，代价是迁移 |
| 包与模块 `spoolkit` | 改成 `spoolkit`（纯机械替换，全仓 220 个文件） |

判断依据：**凡是"用户机器上已经存在的数据"都不动**，凡是"我们自己的命名"都改。

## 任意位置起手

工作区解析（所有子命令共用一套，放在 `workspace.py`）：

1. `--root` 显式给了 → 用它；
2. 否则从当前目录**往上找** `.agent/` → 找到就用那一层（这是"在工作区子目录里
   干活"的场景）；
3. 否则查全局登记表（见下）：只有一个就用它；有多个且人在终端前，列出来让他选；
4. 都没有 → 用当前目录（行为与今天一致，交给具体命令去报它自己的错）。

全局登记表 `%APPDATA%\spoolkit\workspaces.json`（其它平台 `~/.config/spoolkit/`）：
`spool init` 成功时登记工作区绝对路径。于是

```bash
spool approve 7K4M2Q        # 在任意目录都能定位到工作区
```

`--root` 的默认值从 `"."` 改成 `None`，解析统一在 `main()` 里做一次——否则十来个
子命令各写各的 `Path(args.root).resolve()`，规则迟早分叉。

## 命令

```bash
spool                       # 不是工作区就问一句"在这里初始化吗"，然后进对话
spool init [路径]           # 显式初始化（幂等），并登记
spool approve <码>          # 手机配对批准（bridge --approve 的顶层快捷方式）
spool run|plan|chat|bridge|serve|mcp|bench|session|config|limits|diagnose|policy|revert
```

`bridge --approve` 保留（脚本兼容），与 `spool approve` 走同一段代码。

## 文档

- README 重写：定位一句话 + 30 秒上手 + 命令表（现在的版本偏设计说明，作为
  README 第一屏太重）
- 新增 `docs/getting-started.md`：安装、首次向导、任意目录初始化、供应商与代理
- 新增 `docs/cli.md`：每个子命令、每个参数，逐条
- 新增 `docs/manual.md`：使用手册（工作区／三档权限／记忆与归档／长任务与
  子智能体／通道接入／排错）
- 现有 `benchmarking.md`、`bridge.md`、`mcp.md`、`dogfooding.md`：更新命令名与
  交叉引用，内容不动

## 发布

- 仓库 public，`baiyireng/spoolkit`，MIT（LICENSE 已在）
- `pyproject.toml` 补 `[project.urls]`、分类器与描述
- 推 GitHub 需要认证：本机原本没有 `gh` 也没有 token（用户自行安装 `gh`）
- **不发 PyPI**（用户明确说过先不发），但发行名先定下来，免得被别人占

## 影响面

- 机械替换：220 个文件里的 `spoolkit`/`spool`
- 行为改动集中在两处：`--root` 的解析规则、无参数启动
- 验收：全套测试通过（当前 1133）、3.12 干净环境再跑一遍、真机 `spool chat` 与
  `spool approve` 各走一次
