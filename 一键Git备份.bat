@echo off
setlocal
chcp 65001 >nul

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\git_backup.ps1" %*

echo.
pause
