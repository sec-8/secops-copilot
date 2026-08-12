# run.ps1 - SecOps Copilot launcher
# Usage: run  .\run.ps1  in the project root

Set-Location $PSScriptRoot

# Activate virtual environment
if (Test-Path ".\.venv\Scripts\Activate.ps1") {
    .\.venv\Scripts\Activate.ps1
} else {
    Write-Host "[!] venv not found. Run first-time setup:" -ForegroundColor Yellow
    Write-Host "    uv venv" -ForegroundColor Cyan
    Write-Host "    uv pip install -r requirements.txt" -ForegroundColor Cyan
    exit 1
}

# Check .env
if (-not (Test-Path ".\.env")) {
    Write-Host "[!] .env not found, copying from template..." -ForegroundColor Yellow
    Copy-Item ".env.example" ".env"
    Write-Host "[i] .env created. Fill in your API Key then re-run." -ForegroundColor Green
    exit 1
}

# Start service
Write-Host "[+] Starting SecOps Copilot ..." -ForegroundColor Green
Write-Host "[i] Docs: http://127.0.0.1:8000/docs" -ForegroundColor Cyan
uvicorn app.main:app --reload
