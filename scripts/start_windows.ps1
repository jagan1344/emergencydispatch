# Starts backend, simulator and frontend in three new PowerShell windows (PostgreSQL + Mosquitto must be running).
$root = Split-Path -Parent $PSScriptRoot
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$root\backend'; ..\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000"
Start-Sleep -Seconds 6
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$root'; .\.venv\Scripts\python.exe simulator\run_simulator.py --target-routes 0.2"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$root\frontend'; npm run dev"
Write-Host "Open http://localhost:5173  (API docs: http://localhost:8000/docs)"
