@echo off
rem Двойной клик: запускает start.ps1 в обход политики выполнения скриптов.
rem Аргументы прокидываются дальше: start.cmd -Preview
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if errorlevel 1 pause
