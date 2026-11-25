<#
Run the test suite (Windows-habit entry point).

**Environment setup lives in exactly one place**: `scripts/dev_env.py`.

This script used to set five things itself (PYTHONPATH, the sandbox tmpdir
compat plugin, a temp root outside the repo, rg on PATH, the hang guard),
and `scripts/gate.py` duplicated all five — so the same class of trap had to
be re-checked in two places. Now this file is a thin shell:

    scripts\pytest.ps1                                     # main checkout
    scripts\pytest.ps1 -Repo .worktrees\ws-session-kernel   # a worktree
    scripts\pytest.ps1 tests\kernel                         # a subset (args pass through)

WHY THIS FILE IS ASCII-ONLY: Windows PowerShell 5.1 reads a BOM-less file as
ANSI. These .ps1 files have no BOM (the editor strips it), and CJK bytes decoded
as ANSI can swallow the closing quote of a string -> parse error. The Chinese
explanations live in `scripts/dev_env.py` and `src/spoolkit/sandbox_dirs.py`,
which are read as UTF-8 by Python.

Preferred entry point is still `python scripts/gate.py` (doctor + tests + types,
one command on both platforms).
#>
[CmdletBinding()]
param(
    # Repository to test (defaults to the repo this script lives in).
    [string] $Repo = (Split-Path -Parent $PSScriptRoot),
    # Everything else is forwarded to pytest verbatim.
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
    throw "No python in .venv (looked in $mainRepo and $Repo). Run: uv sync --extra dev"
}

# Environment preparation (dir patch, in-repo caches, temp root, rg, plugins): one place.
$envLines = & $python (Join-Path $PSScriptRoot 'dev_env.py') --print-env
foreach ($line in $envLines) {
    $index = $line.IndexOf('=')
    if ($index -gt 0) {
        Set-Item -Path ("env:" + $line.Substring(0, $index)) -Value $line.Substring($index + 1)
    }
}
# Do not override a caller-provided progress log (concurrent runs point at their own).
if (-not $env:PYTEST_HANG_LOG) {
    $env:PYTEST_HANG_LOG = Join-Path $mainRepo '.tmp\pytest-progress.log'
}
$logDir = Split-Path -Parent $env:PYTEST_HANG_LOG
if ($logDir) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }

if (-not (Get-Command rg -ErrorAction SilentlyContinue)) {
    Write-Warning "rg not on PATH and not in the usual places - test_search* / test_view / test_verify will be red; that is not a regression."
}

Write-Host "repo     : $Repo"
Write-Host "python   : $python"
Write-Host "temproot : $env:PYTEST_DEBUG_TEMPROOT"
Write-Host "progress : $env:PYTEST_HANG_LOG   (last line names a hung test; per-test timeout defaults to 90s)"
Write-Host ""

Push-Location $Repo
try {
    & $python -m pytest -q -p no:cacheprovider -p pytest_sandbox_compat -p pytest_hang_guard -p pytest_needs @PytestArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
