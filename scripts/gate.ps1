<#
Thin shell for the repo gate (for people who type .ps1 out of habit).
The real implementation is `scripts/gate.py` - plain Python, works on both platforms.

    scripts\gate.ps1                    # doctor + tests + types
    scripts\gate.ps1 --only doctor      # environment doctor only
    scripts\gate.ps1 --only types       # type check only
    scripts\gate.ps1 --update-baseline  # refresh the type-check baseline

Arguments are forwarded to `gate.py` verbatim.

ASCII-only on purpose: Windows PowerShell 5.1 reads a BOM-less file as ANSI, and
CJK bytes then can swallow a closing quote (see the note in scripts\pytest.ps1).
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $GateArgs
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw "No python in .venv ($python). Run: uv sync --extra dev"
}

Push-Location $repo
try {
    & $python (Join-Path $PSScriptRoot 'gate.py') @GateArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
