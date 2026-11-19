# AetherOS one-line installer for Windows (PowerShell 5.1+).
#
# Run it inside your project folder:
#   irm https://raw.githubusercontent.com/deivycom21-cell/aether-os/main/install.ps1 | iex
#
# What it does (nothing else, nothing outside your user profile + the project):
#   1. installs uv (Astral's Python manager) if it is missing
#   2. installs git with winget if it is missing (skip with $env:AETHER_SKIP_GIT = "1")
#   3. installs AetherOS as an isolated tool (uv downloads Python 3.12 if needed)
#   4. runs `aether setup`: index, MCP for every detected IDE, pre-commit guard, rules, live check
#
# Options (set before running):  $env:AETHER_REF = "feat/v0.2-mega-upgrade"   # branch / tag
#                                 $env:AETHER_SOURCE = "C:\path\to\aether-os"  # local checkout or URL
#                                 $env:AETHER_SETUP_ARGS = "--all --no-hooks"  # extra `aether setup` flags

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Write-Step([string]$text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Add-SessionPath([string]$dir) {
    if ($dir -and (Test-Path $dir) -and -not (($env:Path -split ";") -contains $dir)) { $env:Path = "$dir;$env:Path" }
}

function Install-Aether {
    $ref = if ($env:AETHER_REF) { $env:AETHER_REF } else { "main" }
    $source = if ($env:AETHER_SOURCE) { $env:AETHER_SOURCE } else { "https://github.com/deivycom21-cell/aether-os/archive/refs/heads/$ref.zip" }

    Write-Host "AETHER//OS installer" -ForegroundColor Magenta

    Write-Step "uv"
    $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    if (-not $uv) {
        Write-Host "uv not found, installing it from astral.sh ..."
        & powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
        if ($LASTEXITCODE -ne 0) { throw "uv installation failed" }
        foreach ($dir in @($env:UV_INSTALL_DIR, $env:XDG_BIN_HOME, "$env:USERPROFILE\.local\bin", "$env:USERPROFILE\.cargo\bin")) {
            Add-SessionPath $dir
        }
        $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
        if (-not $uv) { throw "uv was installed but is not on PATH; open a new terminal and run this command again" }
    }
    Write-Host "uv: $uv"

    Write-Step "git"
    if (Get-Command git -ErrorAction SilentlyContinue) {
        Write-Host "git: $((Get-Command git).Source)"
    } elseif ($env:AETHER_SKIP_GIT -eq "1") {
        Write-Host "git missing (AETHER_SKIP_GIT=1): hooks, hotspot churn and semantic diff will be disabled" -ForegroundColor Yellow
    } elseif (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "git not found, installing it with winget ..."
        & winget install --id Git.Git -e --source winget --silent --accept-package-agreements --accept-source-agreements
        Add-SessionPath "$env:ProgramFiles\Git\cmd"
        if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
            Write-Host "git could not be installed automatically; get it from https://git-scm.com and re-run" -ForegroundColor Yellow
        }
    } else {
        Write-Host "git not found and winget is unavailable; install it from https://git-scm.com (AetherOS works without it, with fewer features)" -ForegroundColor Yellow
    }

    Write-Step "AetherOS ($source)"
    & $uv tool install --force --python 3.12 --reinstall-package aether-os $source
    if ($LASTEXITCODE -ne 0) { throw "uv tool install failed" }
    & $uv tool update-shell *> $null
    $bin = (& $uv tool dir --bin).Trim()
    Add-SessionPath $bin
    $aether = Join-Path $bin "aether.exe"
    if (-not (Test-Path $aether)) { throw "aether.exe not found in $bin" }
    & $aether version

    Write-Step "aether setup in $((Get-Location).Path)"
    $setupArgs = @()
    if ($env:AETHER_SETUP_ARGS) { $setupArgs = $env:AETHER_SETUP_ARGS -split "\s+" | Where-Object { $_ } }
    & $aether setup @setupArgs
    if ($LASTEXITCODE -eq 2) {
        Write-Host "`nAetherOS is installed. Open your project folder and run:  aether setup" -ForegroundColor Yellow
    } elseif ($LASTEXITCODE -ne 0) {
        Write-Host "`naether setup reported problems (see above). Re-run it any time with:  aether setup" -ForegroundColor Yellow
    }
    Write-Host "`nNew terminals will find 'aether' on PATH automatically." -ForegroundColor DarkGray
}

Install-Aether
