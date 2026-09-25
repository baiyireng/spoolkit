<#
跑全套测试。把本机（Windows + 文件沙箱）会拦住的四件事一次设好。

    scripts\pytest.ps1                                  # 跑主 checkout
    scripts\pytest.ps1 -Repo .worktrees\ws-session-kernel  # 跑某个 worktree
    scripts\pytest.ps1 tests\kernel                      # 只跑一部分（参数原样透传）

为什么需要它——四条都是踩过的坑，别再手抄漏一条：

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

# ② 插件：本进程 + 派生出去的 pytest 子进程都要带
$env:PYTEST_ADDOPTS = '-p pytest_sandbox_compat'

# ③ 临时根：仓库外、每次新建
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("spoolkit-pytest-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
$env:PYTEST_DEBUG_TEMPROOT = $tempRoot

# ④ rg：PATH 里没有再去找常见位置
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
Write-Host ""

Push-Location $Repo
try {
    & $python -m pytest -q -p no:cacheprovider -p pytest_sandbox_compat @PytestArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
