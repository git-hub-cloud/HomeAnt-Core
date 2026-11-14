#Requires -Version 5.1
<#
.SYNOPSIS
    HomeAnt local gate - run this before calling any change done.

.DESCRIPTION
    Two steps, same as check.sh:

      1. syntax-check every .py this repo owns (in memory: no __pycache__ residue)
      2. run this directory's tests under pytest

    Lives in the repo's tests/ directory and is version-managed. It is dev
    tooling, so it stays out of the release zip (dist-pack.py uses a white list).
    Run it from the repo root, or set HOMEANT_ROOT.

    Both steps are subcommands of verify.py, not PowerShell here: the uv
    discovery and the `--with` list are one fact that check.sh and check.ps1
    would otherwise each spell out, and two spellings of "how to run the tests"
    is the drift the gate itself exists to prevent.

    pytest is deliberately NOT a project dependency.  Putting it in
    pyproject.toml would ship a test runner inside the runtime; installing it
    into .venv by hand is the `uv pip install` drift uv.lock cannot see - it
    resolves the newest matching version instead of the locked one.

    `uv run --no-project` builds a throwaway environment from uv's cache and
    leaves .venv (the HA runtime) untouched.  --with pyyaml / voluptuous are
    there because the seed merge and the voice component import them.

.PARAMETER Python
    Interpreter used for both steps. Defaults to `python`.
#>
[CmdletBinding()]
param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$GateDir = $PSScriptRoot
$RepoRoot = if ($env:HOMEANT_ROOT) { $env:HOMEANT_ROOT } else { (Get-Location).Path }

if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot "scripts/homeant.py"))) {
    Write-Error "check: no scripts/homeant.py under $RepoRoot - run me from the repo root, or set HOMEANT_ROOT"
    exit 1
}
Set-Location -LiteralPath $RepoRoot

Write-Output "== compile =="
& $Python (Join-Path $GateDir "verify.py") --compile
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Output "== pytest =="
& $Python (Join-Path $GateDir "verify.py") --pytest
exit $LASTEXITCODE
