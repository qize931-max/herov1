@echo off
REM Double-click to build a client package to hand to someone.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0make_client.ps1"
pause
