# =====================================================================
#  Create an ISOLATED Hero SMS instance.
#  Each instance gets its own folder, its own login accounts, its own
#  config, its own Chrome/Facebook sessions, its own bot, and its own
#  results - fully separate from every other instance.
#
#  Run this once per person you want to give a separate server to.
# =====================================================================
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "=================================================="
Write-Host "  Create a new ISOLATED Hero SMS instance"
Write-Host "=================================================="
Write-Host ""

$name = Read-Host "Instance name (e.g. client1)"
if ([string]::IsNullOrWhiteSpace($name)) { Write-Host "No name given. Aborting."; exit 1 }
$name = $name.Trim()

$port = Read-Host "Web port for this instance (e.g. 5001, 5002 ...)"
if ($port -notmatch '^\d+$') { Write-Host "Port must be a number. Aborting."; exit 1 }

$dest = Join-Path $root "instances\$name"
$codeFiles = @(
    'app.py',
    'hero_sms_automation.py',
    'clean_unused_profiles.py',
    'analyze_recovery_times.py',
    'requirements.txt'
)

if (Test-Path (Join-Path $dest 'app.py')) {
    Write-Host "Instance '$name' already exists -> updating its code files only."
    Write-Host "(Its accounts, config and results are left untouched.)"
} else {
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    Write-Host "Created folder: $dest"
}

foreach ($f in $codeFiles) {
    $src = Join-Path $root $f
    if (Test-Path $src) { Copy-Item $src -Destination $dest -Force }
}

# Per-instance one-click launcher. %~dp0 stays literal (this is a here-string;
# only $name / $port are substituted by PowerShell).
$startBat = @"
@echo off
title Hero SMS - $name (port $port)
cd /d "%~dp0"
set HERO_HOST=0.0.0.0
set HERO_PORT=$port
echo Starting instance "$name" on port $port ...
echo Open http://localhost:$port  (default login admin / admin)
python app.py $port
pause
"@
Set-Content -Path (Join-Path $dest 'start.bat') -Value $startBat -Encoding ASCII

Write-Host ""
Write-Host "Done. Instance '$name' is ready."
Write-Host "  Folder : $dest"
Write-Host "  Start  : double-click  $dest\start.bat"
Write-Host "  Open   : http://localhost:$port   (login admin / admin, then change the password)"
Write-Host ""
Write-Host "Each instance is fully separate - its own owner login, its own kill switch,"
Write-Host "its own Chrome/Facebook sessions and its own recovered accounts."
Write-Host ""
