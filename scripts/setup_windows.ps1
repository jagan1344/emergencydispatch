# One-time local setup on Windows (PowerShell). Run from the emergency-dispatch folder:
#   Set-ExecutionPolicy -Scope Process Bypass; .\scripts\setup_windows.ps1
$ErrorActionPreference = "Stop"
if (-not (Test-Path .env)) { Copy-Item .env.example .env; Write-Host "Created .env from .env.example - review it." }
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Push-Location backend
..\.venv\Scripts\python.exe -m app.ml.train
..\.venv\Scripts\python.exe -m app.migrate
..\.venv\Scripts\python.exe -m app.seed
Pop-Location
Push-Location frontend
npm install
Pop-Location
Write-Host "Setup complete. See README 'Running the project' for the start commands."
