@echo off
cd /d "%~dp0"

call venv\Scripts\activate

start "" python "observatory\server.py"

timeout /t 1 /nobreak >nul

start http://127.0.0.1:8080