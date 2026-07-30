@echo off
rem Preview run: prints to console, sends nothing. Window stays open.
chcp 65001 >nul
cd /d "%~dp0"
title Digest preview

powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\run.ps1" -Mode dry

echo.
echo === Finished. Scroll up to read. Press any key to close. ===
pause >nul
