# =====================================================================
#  Build the bundled Python runtime (one-time, per device).
#  Creates a self-contained Python 3.12 in .\runtime with all deps, so
#  make_client can produce BUNDLED client packages (client needs nothing).
#
#  Run once after cloning the repo on a new device:
#     powershell -ExecutionPolicy Bypass -File build_runtime.ps1
# =====================================================================
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$rt = Join-Path $root "runtime"

if (Test-Path (Join-Path $rt "python.exe")) {
    Write-Host "runtime already exists at $rt - delete it to rebuild. Nothing to do."
    exit 0
}

$pyver = "3.12.9"
$zipUrl = "https://www.python.org/ftp/python/$pyver/python-$pyver-embed-amd64.zip"
$zipPath = Join-Path $root "pyembed.zip"

Write-Host "Downloading embeddable Python $pyver ..."
Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath

Write-Host "Extracting to $rt ..."
if (Test-Path $rt) { Remove-Item $rt -Recurse -Force }
Expand-Archive -Path $zipPath -DestinationPath $rt -Force
Remove-Item $zipPath -Force

Write-Host "Enabling site-packages (pip) ..."
$pth = Get-ChildItem $rt -Filter "python*._pth" | Select-Object -First 1
(Get-Content $pth.FullName) -replace '^#\s*import site', 'import site' | Set-Content $pth.FullName -Encoding ASCII

Write-Host "Installing pip ..."
$py = Join-Path $rt "python.exe"
$getpip = Join-Path $rt "get-pip.py"
Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getpip
& $py $getpip --no-warn-script-location -q
Remove-Item $getpip -Force

Write-Host "Installing app dependencies (flask waitress pyperclip playwright) ..."
& $py -m pip install --no-warn-script-location -q flask waitress pyperclip playwright

Write-Host "Verifying ..."
& $py -c "import flask, waitress, pyperclip; from playwright.sync_api import sync_playwright; print('runtime OK - bundled packages ready')"

Write-Host ""
Write-Host "Done. 'runtime' is ready. Now make_client.bat will build BUNDLED packages"
Write-Host "(clients need nothing installed)."
