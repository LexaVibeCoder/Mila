@echo off
REM One-shot TDLib login for Mila (Windows).
REM
REM Usage:
REM   start\create_session.bat              (login by Telegram code)
REM   start\create_session.bat --qr         (QR link login, no code)
REM   (run from anywhere; paths are resolved relative to this script;
REM    arguments are passed through to create_session.py)
REM
REM What it does:
REM   1. Drops a stale venv from a previous run; if it cannot be removed
REM      (locked by a running process, another user, etc.) it fails with
REM      a clear message instead of leaving uv/python confused.
REM   2. If Python 3.11 is available (py -3.11 or python 3.11) -> venv with it.
REM      Otherwise -> installs uv (if missing), creates the venv with
REM      `uv venv --python 3.11` (uv downloads Python 3.11 by itself).
REM   3. Makes sure pip exists inside the venv (ensurepip, uv as fallback).
REM   4. Installs only what create_session.py needs, with pytdbot/tdjson
REM      pinned to matching versions.
REM   5. Runs `python create_session.py` from the project root.
REM   6. On success deletes the venv. On failure keeps it for debugging.
setlocal EnableDelayedExpansion

REM Script dir (with trailing backslash) and project root.
set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "PROJECT_ROOT=%%~fI"
set "VENV_DIR=%SCRIPT_DIR%.venv-session"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

set "PYTDBOT_SPEC=Pytdbot[tdjson]==0.10.1"
set "TDJSON_SPEC=tdjson==1.8.65"

echo === Mila one-shot TDLib login ===
echo Project: %PROJECT_ROOT%

REM Fresh start: drop a stale venv from a previous failed run.
call :clean_venv
if errorlevel 1 (
    echo ERROR: could not remove the old venv at: 1>&2
    echo        %VENV_DIR% 1>&2
    echo        Close running Mila/python processes and retry. 1>&2
    exit /b 1
)

set "PYBIN="

REM 1) Try the py launcher with 3.11.
py -3.11 --version >nul 2>&1
if not errorlevel 1 (
    set "PYBIN=py -3.11"
    echo --^> Found system Python 3.11 via py launcher
    goto :make_venv
)

REM 2) Try plain python and check for 3.11 in its version string.
python --version 2>&1 | findstr /C:"3.11" >nul 2>&1
if not errorlevel 1 (
    set "PYBIN=python"
    echo --^> Found system Python 3.11 via python
    goto :make_venv
)

echo --^> System Python 3.11 not available, using uv
goto :via_uv

:make_venv
%PYBIN% -m venv "%VENV_DIR%"
if not errorlevel 1 (
    echo --^> venv created with system Python 3.11
    goto :ensure_pip
)
echo --^> python -m venv failed, falling back to uv
call :clean_venv
if errorlevel 1 (
    echo ERROR: could not remove the incomplete venv at: 1>&2
    echo        %VENV_DIR% 1>&2
    exit /b 1
)

:via_uv
call :ensure_uv
if errorlevel 1 exit /b 1
uv venv --python 3.11 "%VENV_DIR%"
if errorlevel 1 exit /b 1
echo --^> venv created with uv ^(Python 3.11^)

:ensure_pip
if not exist "%VENV_PY%" (
    echo ERROR: venv python not found at %VENV_PY% 1>&2
    exit /b 1
)
"%VENV_PY%" --version
if errorlevel 1 (
    echo ERROR: venv python is not runnable: %VENV_PY% 1>&2
    exit /b 1
)

REM uv venvs ship without pip by default -> install it if missing.
"%VENV_PY%" -m pip --version >nul 2>&1
if not errorlevel 1 goto :install_deps
echo --^> pip missing in venv, installing pip...
"%VENV_PY%" -m ensurepip --upgrade >nul 2>&1
if not errorlevel 1 (
    "%VENV_PY%" -m pip --version >nul 2>&1
    if not errorlevel 1 goto :install_deps
)
call :ensure_uv
if errorlevel 1 exit /b 1
uv pip install --python "%VENV_PY%" pip
if errorlevel 1 (
    echo ERROR: cannot install pip into the venv. 1>&2
    exit /b 1
)
echo --^> pip installed via uv

:install_deps
echo --^> Installing session deps: %PYTDBOT_SPEC% %TDJSON_SPEC% pydantic-settings python-dotenv qrcode
"%VENV_PY%" -m pip install --upgrade pip
if errorlevel 1 exit /b 1
"%VENV_PY%" -m pip install "%PYTDBOT_SPEC%" "%TDJSON_SPEC%" pydantic-settings python-dotenv "qrcode>=7.4"
if errorlevel 1 exit /b 1

echo --^> Running create_session.py ^(workdir: project root^)
cd /d "%PROJECT_ROOT%"
if errorlevel 1 (
    echo ERROR: cannot cd to %PROJECT_ROOT% 1>&2
    exit /b 1
)
"%VENV_PY%" create_session.py %*
set "STATUS=%errorlevel%"

if "%STATUS%"=="0" (
    echo --^> Login OK, removing temporary venv
    call :clean_venv
) else (
    echo --^> create_session.py failed ^(exit %STATUS%^). Venv kept at: 1>&2
    echo     %VENV_DIR% 1>&2
    echo     Re-run this script to start fresh. 1>&2
)

exit /b %STATUS%

REM ---------------------------------------------------------------------------
REM Remove VENV_DIR (directory, or a stray file with the same name).
REM Returns errorlevel 1 if something could not be deleted.
REM ---------------------------------------------------------------------------
:clean_venv
if not exist "%VENV_DIR%" exit /b 0
if exist "%VENV_DIR%\" (
    rmdir /s /q "%VENV_DIR%" 2>nul
) else (
    del /f /q "%VENV_DIR%" 2>nul
)
if exist "%VENV_DIR%" exit /b 1
exit /b 0

REM ---------------------------------------------------------------------------
REM Install uv if it is not on PATH yet.
REM ---------------------------------------------------------------------------
:ensure_uv
where uv >nul 2>&1
if not errorlevel 1 exit /b 0
echo --^> uv not found, installing uv...
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
if errorlevel 1 (
    echo ERROR: uv installation failed. 1>&2
    exit /b 1
)
REM Make uv visible in this session (default install locations).
set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
where uv >nul 2>&1
if errorlevel 1 (
    echo ERROR: uv not found after install. Check %USERPROFILE%\.local\bin 1>&2
    exit /b 1
)
for /f "delims=" %%V in ('uv --version') do echo --^> uv installed: %%V
exit /b 0
