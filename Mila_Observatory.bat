@echo off
cd /d "%~dp0"

set "VENV_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo ERROR: venv not found at %VENV_PY% 1>&2
    echo        Run start\install.bat first. 1>&2
    exit /b 1
)

if not exist "Mila_observatory\frontend\dist\index.html" (
    echo WARNING: Observatory frontend is not built; the panel will start without UI. 1>&2
    echo          Run: cd /d "Mila_observatory\frontend" ^&^& npm ci ^&^& npm run build 1>&2
    echo          Or re-run start\install.bat. 1>&2
)

start "" "%VENV_PY%" -m Mila_observatory.server

timeout /t 3 /nobreak >nul

rem Open the panel on the host/port configured in .env (OBSERVATORY_HOST/OBSERVATORY_PORT).
for /f "usebackq delims=" %%u in (`"%VENV_PY%" -c "from config import settings; from Mila_observatory.auth import resolve_host; h=resolve_host(); h='127.0.0.1' if h=='0.0.0.0' else h; print('http://'+h+':'+str(settings.OBSERVATORY_PORT))"`) do set "OBS_URL=%%u"
if not defined OBS_URL (
    echo ERROR: cannot resolve Observatory URL from .env. 1>&2
    echo        Check OBSERVATORY_HOST/OBSERVATORY_PORT or run start\check_config.py. 1>&2
    exit /b 1
)
start "" "%OBS_URL%"
