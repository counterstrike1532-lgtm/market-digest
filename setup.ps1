# One-time setup. Run from the project folder:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\setup.ps1
# ASCII only on purpose: Windows PowerShell 5.1 misreads UTF-8 files without BOM.

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "=== 1. Python ===" -ForegroundColor Cyan
$pyver = python --version 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "Python not found. Install it:  winget install Python.Python.3.12" -ForegroundColor Red
    Write-Host "Then close PowerShell, open it again, and re-run .\setup.ps1"
    exit 1
}
Write-Host "  $pyver" -ForegroundColor Green

Write-Host ""
Write-Host "=== 2. Project structure ===" -ForegroundColor Cyan
$need = @("src\main.py","src\collect.py","src\numbers.py","src\brain.py",
          "src\deliver.py","src\verify_feeds.py","config\sources.yaml","requirements.txt")
$missing = @($need | Where-Object { -not (Test-Path $_) })
if ($missing.Count -gt 0) {
    Write-Host "  MISSING files:" -ForegroundColor Red
    foreach ($m in $missing) { Write-Host "    $m" -ForegroundColor Red }
    Write-Host "  Unpack the zip again, keeping the folder structure."
    exit 1
}
Write-Host "  all files present" -ForegroundColor Green

Write-Host ""
Write-Host "=== 3. Virtual environment ===" -ForegroundColor Cyan
if (-not (Test-Path ".venv")) { python -m venv .venv }
$py = ".\.venv\Scripts\python.exe"
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -r requirements.txt
if ($LASTEXITCODE -ne 0) { Write-Host "  pip install failed" -ForegroundColor Red; exit 1 }
Write-Host "  dependencies installed" -ForegroundColor Green

Write-Host ""
Write-Host "=== 4. Secrets file ===" -ForegroundColor Cyan
if (-not (Test-Path "secrets.ps1")) {
    $tpl = @(
        '# Put your keys between the quotes, then save this file.',
        '#',
        '# GEMINI_API_KEY     https://aistudio.google.com  ->  Get API key',
        '# TELEGRAM_BOT_TOKEN Telegram: message @BotFather  ->  /newbot',
        '# TELEGRAM_CHAT_ID   message your bot, then open in a browser:',
        '#                    https://api.telegram.org/bot<TOKEN>/getUpdates',
        '#                    and find  "chat":{"id":NUMBER',
        '',
        '$env:GEMINI_API_KEY     = ""',
        '$env:TELEGRAM_BOT_TOKEN = ""',
        '$env:TELEGRAM_CHAT_ID   = ""',
        ''
    )
    # ASCII encoding so PowerShell 5.1 always reads it correctly
    Set-Content -Path "secrets.ps1" -Value $tpl -Encoding ASCII
    Write-Host "  created secrets.ps1 - open it and paste your keys" -ForegroundColor Yellow
} else {
    Write-Host "  secrets.ps1 already exists" -ForegroundColor Green
}

if (-not (Test-Path ".gitignore")) { Set-Content ".gitignore" "" -Encoding ASCII }
if (-not (Select-String -Path ".gitignore" -Pattern "secrets.ps1" -SimpleMatch -Quiet)) {
    Add-Content ".gitignore" "secrets.ps1"
}
Write-Host "  secrets.ps1 is in .gitignore (will not reach git)" -ForegroundColor Green

Write-Host ""
Write-Host "=== Done ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next steps:"
Write-Host ""
Write-Host "  1) Check which RSS feeds are alive (no keys needed):"
Write-Host "       .\run.ps1 verify"
Write-Host ""
Write-Host "  2) Paste your keys into secrets.ps1"
Write-Host ""
Write-Host "  3) Full run, nothing sent to Telegram:"
Write-Host "       .\run.ps1 dry"
Write-Host ""
Write-Host "  4) If the stories look good, send them:"
Write-Host "       .\run.ps1 send"
Write-Host ""
