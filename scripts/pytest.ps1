<#
跑全套测试。把本机（Windows + 文件沙箱）会拦住的五件事一次设好。

    scripts\pytest.ps1                                  # 跑主 checkout
    scripts\pytest.ps1 -Repo .worktrees\ws-session-kernel  # 跑某个 worktree
    scripts\pytest.ps1 tests\kernel                      # 只跑一部分（参数原样透传）

为什么需要它——五条都是踩过的坑，别再手抄漏一条：

1. **PYTHONPATH 必须指向被跑的那份源码。** `spool` 是 editable 装到主
   checkout 的；在 worktree 里不设这条，测试 import 到的是 main 那份 spoolkit
   ——测的是别人的代码，而且会全绿。
2. **pytest 的 tmpdir 在本机沙箱里建不出目录**（0o700 建的目录立刻失去访问权，
   1400+ 条全部 error）。靠 `scripts/pytest_sandbox_compat.py` 抬 mode 解决，
   并用 `PYTEST_ADDOPTS` 传下去——bench 那 50 条会再派生 pytest 子进程。
3. **临时根必须在仓库外**，而且必须是**新建**的空目录。放进仓库内会有两个后果：
   临时目录落在某个工作区里，解析类用例改成"向上找到了 .agent"而失败；顶着
   旧名字重进还可能撞上之前被锁死的目录。
4. **PATH 里要有 rg**（`tools/search.py` 依赖），沙箱给的 PATH 通常只有
   preset-runtime，ripgrep 找不到会连带 `test_search*` / `test_view` / `test_verify`
   一起红——那不是回归。
5. **挂死要变成"有堆栈的失败"，不能把调用方一起拖死。** 用例里有等 stdin、
   等 socket、等子进程退出的东西，挂在一次 pytest 里的代价是三倍：输出被块缓冲、
   杀掉后连"停在哪条"都没留下；跑测试的那条命令不返回，整轮工作丢失；残留进程
   还会毒化后面几次运行。靠 `scripts/pytest_hang_guard.py`（只用标准库的
   `faulthandler`）解决：逐条用例计时，到点打印**全线程堆栈**再退出，并把正在跑的
   用例名逐行 flush 进进度日志。可调：

       $env:PYTEST_HANG_SECONDS = '45'   # 单条用例超时，默认 90 秒
       # 卡住之后：看下面的"进度日志"，里面最后一行就是凶手；
       # 下一轮用 -k 'not <用例名>' 把已判定的挂死项排掉，先让其余部分跑完。

   进度日志的位置**已经有人设了就不覆盖**（`$env:PYTEST_HANG_LOG`）：几个 pytest
   并发跑时共用一份日志，会让"读日志找凶手"的那一方看到别的轮次的 START/END，
   报出根本没收集的用例卡死（`pytest_bounded.ps1` 就是靠这条每轮指自己的文件）。
#>
[CmdletBinding()]
param(
    # 要测的仓库路径（默认本脚本所在仓库）。给 worktree 路径时会用它自己的 src。
    [string] $Repo = (Split-Path -Parent $PSScriptRoot),
    # 透传给 pytest 的其余参数。
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $PytestArgs
)

$ErrorActionPreference = 'Stop'

$mainRepo = Split-Path -Parent $PSScriptRoot
$Repo = (Resolve-Path -LiteralPath $Repo).Path

$python = Join-Path $mainRepo '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    $python = Join-Path $Repo '.venv\Scripts\python.exe'
}
if (-not (Test-Path -LiteralPath $python)) {
    throw "找不到 venv 里的 python（找过 $mainRepo 和 $Repo）。先跑一次 uv venv / uv pip install -e `".[dev]`"。"
}

# ① 被测源码在前，插件目录在后
$env:PYTHONPATH = @((Join-Path $Repo 'src'), $PSScriptRoot) -join ';'
$env:PYTHONIOENCODING = 'utf-8'

# ② 插件：本进程 + 派生出去的 pytest 子进程都要带（挂死防护同理）
$env:PYTEST_ADDOPTS = '-p pytest_sandbox_compat -p pytest_hang_guard'

# ③ 临时根：仓库外、每次新建
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("spoolkit-pytest-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
$env:PYTEST_DEBUG_TEMPROOT = $tempRoot

# ④ 挂死防护：行缓冲（被杀也留证据）+ 进度日志写到仓库里（好读）
$env:PYTHONUNBUFFERED = '1'
# **有人显式设了 PYTEST_HANG_LOG 就不覆盖**：进度日志是"所有并发 pytest 共用一份"
# 的那一类东西，两边往同一个文件里追加 START/END，读日志的那一方就会看到
# "有 START 没有 END"——也就是**根本没收集的用例被报成卡死**（本批真踩过：
# `pytest_bounded.ps1` 只跑了两个文件，却被报出 3 条 `tests/kernel` / `tests/llm`
# 的"卡死"并写进隔离表）。所以让调用方（`pytest_bounded.ps1` 每轮一份自己的日志）
# 能指自己的文件，是这一层唯一的解法。自己跑时才用默认那份。
if (-not $env:PYTEST_HANG_LOG) {
    $env:PYTEST_HANG_LOG = Join-Path $mainRepo '.tmp\pytest-progress.log'
}
# 下面印的是**真正用的那一份**（可能是调用方指的），否则人不知道该去哪儿看堆栈。
$progressLog = $env:PYTEST_HANG_LOG
$logDir = Split-Path -Parent $progressLog
if ($logDir) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }

# ⑤ rg：PATH 里没有再去找常见位置
if (-not (Get-Command rg -ErrorAction SilentlyContinue)) {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\cursor\resources\app\node_modules\@vscode\ripgrep\bin'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links'),
        'C:\Program Files\ripgrep'
    )
    foreach ($dir in $candidates) {
        if (Test-Path -LiteralPath (Join-Path $dir 'rg.exe')) {
            $env:PATH = "$dir;$env:PATH"
            break
        }
    }
    if (-not (Get-Command rg -ErrorAction SilentlyContinue)) {
        Write-Warning "PATH 里没有 rg，也不在常见位置——test_search* / test_view / test_verify 会红，那不是回归。"
    }
}

Write-Host "repo     : $Repo"
Write-Host "python   : $python"
Write-Host "temproot : $tempRoot"
Write-Host "进度日志 : $progressLog   （卡住时看这里最后一行；单条超时 $($env:PYTEST_HANG_SECONDS) 秒可调，默认 90）"
Write-Host ""

Push-Location $Repo
try {
    & $python -m pytest -q -p no:cacheprovider -p pytest_sandbox_compat -p pytest_hang_guard @PytestArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
