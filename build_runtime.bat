@echo off
REM One-time: build the bundled Python runtime so make_client makes bundled packages.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_runtime.ps1"
pause
