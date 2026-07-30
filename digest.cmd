@echo off
rem Digest -> Telegram. Double-click to run.
rem ASCII only: cmd.exe uses OEM codepage and mangles Cyrillic in .cmd files.
chcp 65001 >nul
cd /d "%~dp0"
title Digest

echo Running digest, this takes 1-3 minutes...
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\run.ps1" -Mode send

if errorlevel 1 (
    echo.
    echo ============================================
    echo   ERROR - read the messages above
    echo   Common causes:
    echo     - Gemini daily quota spent, wait for reset
    echo     - no keys in secrets.ps1
    echo ============================================
    echo.
    pause
) else (
    echo.
    echo === Done. Digest sent to Telegram. ===
    timeout /t 6 >nul
)
