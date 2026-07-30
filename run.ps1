# Usage:  .\run.ps1 verify   |   .\run.ps1 dry   |   .\run.ps1 send
# ASCII only on purpose: Windows PowerShell 5.1 misreads UTF-8 files without BOM.

param(
    [Parameter(Mandatory=$true)]
    [ValidateSet("verify","dry","send","models")]
    [string]$Mode
)

$py = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "No virtual environment. Run .\setup.ps1 first." -ForegroundColor Red
    exit 1
}

if ($Mode -ne "verify") {
    if (-not (Test-Path "secrets.ps1")) {
        Write-Host "secrets.ps1 not found. Run .\setup.ps1 first." -ForegroundColor Red
        exit 1
    }
    . .\secrets.ps1
    if (-not $env:GEMINI_API_KEY) {
        Write-Host "GEMINI_API_KEY is empty - paste it into secrets.ps1" -ForegroundColor Red
        exit 1
    }
    if ($Mode -eq "send") {
        if (-not $env:TELEGRAM_BOT_TOKEN) {
            Write-Host "TELEGRAM_BOT_TOKEN is empty - paste it into secrets.ps1" -ForegroundColor Red
            exit 1
        }
        if (-not $env:TELEGRAM_CHAT_ID) {
            Write-Host "TELEGRAM_CHAT_ID is empty - paste it into secrets.ps1" -ForegroundColor Red
            exit 1
        }
    }
}

# UTF-8 output so Cyrillic in the digest prints correctly in the console
$env:PYTHONIOENCODING = "utf-8"
$OutputEncoding = [System.Text.Encoding]::UTF8
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

switch ($Mode) {
    "verify" { & $py -m src.verify_feeds }
    "models" { & $py -m src.list_models }
    "dry"    { & $py -m src.main --dry --hours 48 }
    "send"   { & $py -m src.main --hours 48 }
}
