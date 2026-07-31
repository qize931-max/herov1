@echo off
REM Double-click this to create a new isolated Hero SMS instance.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0new_instance.ps1"
pause
