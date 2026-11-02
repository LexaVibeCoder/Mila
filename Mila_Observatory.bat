@echo off
cd /d "%~dp0"

call venv\Scripts\activate

start "" python "Mila_observatory\server.py"

timeout /t 3 /nobreak >nul

rem Open the panel on the host/port configured in .env (OBSERVATORY_HOST/OBSERVATORY_PORT).
for /f "usebackq delims=" %%u in (`python -c "from config import settings; from Mila_observatory.auth import resolve_host; h=resolve_host(); h='127.0.0.1' if h=='0.0.0.0' else h; print('http://'+h+':'+str(settings.OBSERVATORY_PORT))"`) do set "OBS_URL=%%u"
if not defined OBS_URL (
    echo ERROR: cannot resolve Observatory URL from .env. 1>&2
    echo        Check OBSERVATORY_HOST/OBSERVATORY_PORT or run start\check_config.py. 1>&2
    exit /b 1
)
start "" "%OBS_URL%"