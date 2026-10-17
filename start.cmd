@echo off
rem Double-click launcher for start.ps1 (bypasses execution policy).
rem Extra arguments are passed through: start.cmd -Preview
setlocal
chcp 65001 >nul

set "PS_EXE=powershell"
where pwsh >nul 2>nul && set "PS_EXE=pwsh"

"%PS_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" pause
exit /b %CODE%
