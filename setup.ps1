# Set up claude-blockers from a clone, on Windows.
#
#   .\setup.ps1
#   .\setup.ps1 --home D:\blockers
#
# Anything you pass is handed to `claude-blockers install`.
#
# If PowerShell refuses to run this, it is the execution policy, not the script:
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# uv colours its output even when it is being captured, and those escape codes
# end up inside the path we read back out of it -- Join-Path then goes looking
# for a drive called "<esc>[36mC". Ask for plain text instead.
$env:NO_COLOR = "1"

function Say  { param($m) Write-Host $m -ForegroundColor Cyan }
function Warn { param($m) Write-Host $m -ForegroundColor Yellow }

# --------------------------------------------------------------------- uv
# The installer updates the *user* PATH, which this already-running process will
# never see, and different uv versions land in different directories. So look in
# the places it actually uses rather than trusting PATH alone.
function Find-Uv {
  $cmd = Get-Command uv -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  foreach ($dir in @("$env:USERPROFILE\.local\bin", "$env:USERPROFILE\.cargo\bin")) {
    $candidate = Join-Path $dir "uv.exe"
    if (Test-Path $candidate) {
      $env:PATH = "$dir;$env:PATH"
      return $candidate
    }
  }
  return $null
}

if (-not (Find-Uv)) {
  Say "uv not found -- installing it (this is the only dependency)."
  try {
    # Some Windows builds still negotiate TLS 1.0 by default, which astral.sh
    # refuses; the download then fails with a bare "could not create SSL/TLS
    # secure channel" that names nothing you could act on.
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
  } catch {
    Write-Host ""
    Warn "Could not install uv automatically: $($_.Exception.Message)"
    Warn "Install it yourself and re-run this script. Either of these works:"
    Write-Host "    winget install --id=astral-sh.uv -e"
    Write-Host "    https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
  }
  if (-not (Find-Uv)) {
    Write-Host ""
    Warn "uv installed, but this shell still cannot see it -- PowerShell reads"
    Warn "PATH once, at startup, and the installer changed it afterwards."
    Warn "Open a NEW terminal and re-run this script. Nothing is broken."
    exit 1
  }
}

# ------------------------------------------------------------- running board
# Windows will not let anyone overwrite a file that is open, and `uv tool
# install` finishes by copying its launcher over ~\.local\bin\claude-blockers.exe
# -- which is the very file a running board is executing. The upgrade then dies
# on "os error 32: the process cannot access the file", naming a path and no
# reason. Everything before that point has already been installed, so the
# failure is also misleading about how much of it happened.
$running = Get-CimInstance Win32_Process -Filter "Name = 'claude-blockers.exe'" -ErrorAction SilentlyContinue
if ($running) {
  Warn "The board is running, and Windows will not let this overwrite its"
  Warn "launcher while it is open. Stop it and re-run this script:"
  Write-Host ""
  Write-Host "    Get-Process claude-blockers | Stop-Process"
  Write-Host ""
  Warn "Nothing has been changed yet. Your board and its database are untouched."
  exit 1
}

# ------------------------------------------------------------------ install
Say "Installing claude-blockers from this checkout..."
# --reinstall-package is what makes `git pull && ./setup.sh` actually upgrade.
# --force replaces the tool environment but still accepts a wheel uv already
# built for this version from this path, and the version does not change between
# commits -- so an upgrade would reinstall byte-identical stale code and report
# success. Naming the package rebuilds ours without rebuilding the dependency
# tree behind it.
uv tool install --force --reinstall-package claude-blockers .
if ($LASTEXITCODE -ne 0) { throw "uv tool install failed" }

# Deliberately no `2>$null` here. Under Windows PowerShell 5.1 with
# $ErrorActionPreference = "Stop", redirecting a native command's stderr turns
# each line into a terminating error -- so an older uv that does not know --bin,
# or any passing deprecation notice, would kill the script instead of falling
# through to the default below.
$bin = $null
try { $bin = & uv tool dir --bin } catch { $bin = $null }
if ($LASTEXITCODE -ne 0) { $bin = $null }
# Strip any escape codes that survived NO_COLOR, and trim: a stray newline is
# just as fatal to Join-Path as a colour code.
# [char]27 rather than `e, which Windows PowerShell 5.1 does not understand.
if ($bin) { $bin = ($bin -replace "$([char]27)\[[0-9;]*m", "").Trim() }
if (-not $bin) { $bin = "$env:USERPROFILE\.local\bin" }
$exe = Join-Path $bin "claude-blockers.exe"
if (-not (Test-Path $exe)) { throw "expected $exe to exist after install" }

# --------------------------------------------------------------- wire it up
Say "Wiring it into Claude Code..."
& $exe install @args
if ($LASTEXITCODE -ne 0) { throw "claude-blockers install failed" }

# ------------------------------------------------------------------- finish
# uv can put its own shim directory on PATH permanently. Doing it here saves the
# user a step they would otherwise have to read about and run themselves.
if (-not (Get-Command claude-blockers -ErrorAction SilentlyContinue)) {
  try { & uv tool update-shell 2>&1 | Out-Null } catch { }
}

Write-Host ""
if (Get-Command claude-blockers -ErrorAction SilentlyContinue) {
  Say "Done. Start the board with:  claude-blockers serve"
} else {
  Warn "Done -- installed, but this shell's PATH predates it."
  Warn "$bin is now on your PATH for NEW terminals."
  Warn "Open one, then run:  claude-blockers serve"
  Write-Host ""
  Write-Host "  In this shell, the full path still works:"
  Write-Host "    $exe serve"
}
Write-Host "Restart any Claude Code sessions that are already running."
