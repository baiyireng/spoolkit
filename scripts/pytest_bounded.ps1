<#
跑测试，而且**卡死也能自己收场**：自动认出挂住的那条、把它隔离、把其余部分跑完。

    scripts\pytest_bounded.ps1 -Repo .worktrees\x tests\cli        # 默认单条 20 秒
    scripts\pytest_bounded.ps1 -Repo .worktrees\x -HangSeconds 45 tests

为什么要有它（这是"自动检测卡死并自行解决"的那一半，另一半在 pytest_hang_guard.py）：

`pytest_hang_guard.py` 解决的是"**挂在一次 pytest 里，把调用方一起拖死、还一点证据不留**"
——它逐条计时、到点打印全线程堆栈并退出，同时把正在跑的用例名逐行 flush 进进度日志。
但"退出了"还不等于"查清了"：一次挂死会让**整套跑不完**，而人（或 agent）往往只想
"先把没挂的那些跑完，看看还有没有别的问题"。这个脚本就干这件事：

1. 跑一轮（带防护）；
2. 读进度日志：**最后一行是"开始 X"而没有对应的"结束 X"** → X 就是挂死项；
3. 把 X 追加记进 `-QuarantineFile`（默认 `.tmp\pytest-quarantine.txt`，带时间戳与原因），
   然后 `-k "not X"` 重跑，最多 `-Rounds` 轮（默认 3）——一轮排掉一个挂死项；
4. 最后打印：隔离了哪些、其余部分的**真实**通过/失败情况。

"自行解决"的边界要说清：它**不修**挂死的用例，也不假装没发生——只是把"一条挂死拖垮整轮"
变成"一条挂死被记账、其余照跑"。要不要修、怎么修，看堆栈（进度日志里有）。

⚠️ 本文件是 `.ps1`：**必须带 UTF-8 BOM**，否则 PowerShell 5.1 会把中文注释当 ANSI 读、直接语法错。
用编辑器/工具重写它之后，记得补 BOM（见 docs/roadmap.md §9）。
#>
[CmdletBinding()]
param(
    [string] $Repo = (Split-Path -Parent $PSScriptRoot),
    # 单条用例超时（秒）：传给 pytest_hang_guard。
    [int] $HangSeconds = 20,
    # 最多隔离几轮（一轮排掉一个挂死项）。
    [int] $Rounds = 3,
    [string] $QuarantineFile = '',
    # 剩下的都透传给 pytest。**显式占住位置 0**：否则裸参数会按声明顺序落到
    # -HangSeconds / -Rounds 上，症状是"无法将值 tests\cli\... 转换为 Int32"。
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]] $PytestArgs
)

$ErrorActionPreference = 'Stop'
$mainRepo = Split-Path -Parent $PSScriptRoot
if (-not $QuarantineFile) { $QuarantineFile = Join-Path $mainRepo '.tmp\pytest-quarantine.txt' }
$progressLog = Join-Path $mainRepo '.tmp\pytest-progress.log'
$runner = Join-Path $PSScriptRoot 'pytest.ps1'

$excluded = New-Object System.Collections.ArrayList

for ($round = 1; $round -le $Rounds; $round++) {
    $env:PYTEST_HANG_SECONDS = "$HangSeconds"
    Remove-Item -LiteralPath $progressLog -ErrorAction SilentlyContinue

    $extra = @()
    if ($excluded.Count -gt 0) {
        # 用 `--deselect <完整 nodeid>`，**不要用 `-k`**：`-k` 只匹配用例名/类名，
        # 喂完整 nodeid 时它谁都不匹配，于是"not 谁都不匹配" = 全选，挂死那条照样跑
        # （本批踩过：连续三轮都隔离了同一条）。
        $extra = @($excluded | ForEach-Object { '--deselect'; $_ })
        Write-Host "第 $round 轮：已隔离 $($excluded.Count) 条（$($excluded -join '、')）"
    } else {
        Write-Host "第 $round 轮：先跑一遍（单条超时 ${HangSeconds}s）"
    }

    # 注意别用 `$args`：那是 PowerShell 的自动变量，赋值与展开都不按你想的来
    # （踩过：`-Repo` 被当成位置参数的值传进去，报"找不到路径 -Repo"）。
    & $runner -Repo $Repo @extra @PytestArgs
    $code = $LASTEXITCODE

    # 找出"开始了但没有结束"的那一条。
    # ⚠️ 标记是 ASCII（START/END），而且这里必须显式 `-Encoding utf8`：默认编码会把
    # UTF-8 的用例名读成乱码——标记若也用中文，比较会全部失配、**误报"没有卡死"**
    # （本批真踩过：驱动脚本说"正常收场"，而日志里明明有一条 START 没有 END）。
    $culprit = $null
    if (Test-Path -LiteralPath $progressLog) {
        $started = @{}
        foreach ($line in (Get-Content -LiteralPath $progressLog -Encoding utf8)) {
            if ($line -like 'START *') {
                $name = $line.Substring(6).Trim()
                $started[$name] = $true
            } elseif ($line -like 'END *') {
                $name = $line.Substring(4).Trim()
                $started.Remove($name) | Out-Null
            }
        }
        if ($started.Count -gt 0) { $culprit = @($started.Keys)[0] }
    }

    if (-not $culprit) {
        Write-Host ""
        Write-Host "第 $round 轮正常收场（退出码 $code），没有发现卡死的用例。"
        if ($excluded.Count -gt 0) {
            Write-Host "本次隔离过：$($excluded -join '、')（记在 $QuarantineFile）"
        }
        exit $code
    }

    Write-Warning "卡死：$culprit —— 隔离它，继续跑其余部分"
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -LiteralPath $QuarantineFile -Encoding utf8 `
        -Value "$stamp`t$culprit`t单条超时 ${HangSeconds}s 到点未返回（堆栈见 $progressLog）"
    $excluded.Add($culprit) | Out-Null
}

Write-Warning "连续 $Rounds 轮都还有卡死的用例，停下。隔离记录：$QuarantineFile"
exit 1
