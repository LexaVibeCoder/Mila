@echo off
REM One-shot TDLib login for Mila (Windows).
REM
REM Usage:
REM   start\create_session.bat
REM   (run from anywhere; paths are resolved relative to this script)
REM
REM What it does:
REM   1. If Python 3.11 is available (py -3.11 or python 3.11) -> venv with it.
REM      Otherwise -> installs uv (if missing), creates the venv with
REM      `uv venv --python 3.11` (uv downloads Python 3.11 by itself)
REM      and installs pip into it.
REM   2. Installs only what create_session.py needs:
REM      pytdbot (with prebuilt tdjson), pydantic-settings, python-dotenv.
REM   3. Runs `python create_session.py` from the project root.
REM   4. On success deletes the venv. On failure keeps it for debugging.
setlocal EnableDelayedExpansion

REM Script dir (with trailing backslash) and project root.
set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "PROJECT_ROOT=%%~fI"
set "VENV_DIR=%SCRIPT_DIR%.venv-session"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

echo === Mila one-shot TDLib login ===
echo Project: %PROJECT_ROOT%

REM Fresh start: drop a stale venv from a previous failed run.
if exist "%VENV_DIR%" rmdir /s /q "%VENV_DIR%"

set "PYBIN="
set "HAVE_UV_VENV=0"

REM 1) Try the py launcher with 3.11.
py -3.11 --version >nul 2>&1
if %errorlevel% equ 0 (
    set "PYBIN=py -3.11"
    echo --^> Found system Python 3.11 via py launcher
    goto :make_venv
)

REM 2) Try plain python and check for 3.11 in its version string.
python --version 2>&1 | findstr /C:"3.11" >nul 2>&1
if %errorlevel% equ 0 (
    set "PYBIN=python"
    echo --^> Found system Python 3.11 via python
    goto :make_venv
)

echo --^> System Python 3.11 not available, using uv
goto :via_uv

:make_venv
%PYBIN% -m venv "%VENV_DIR%"
if %errorlevel% equ 0 (
    echo --^> venv created with system Python 3.11
    goto :ensure_pip
)
echo --^> python -m venv failed, falling back to uv

:via_uv
call :ensure_uv
if %errorlevel% neq 0 exit /b 1
uv venv --python 3.11 "%VENV_DIR%"
if %errorlevel% neq 0 exit /b 1
echo --^> venv created with uv ^(Python 3.11^)
set "HAVE_UV_VENV=1"

:ensure_pip
if not exist "%VENV_PY%" (
    echo ERROR: venv python not found at %VENV_PY% 1>&2
    exit /b 1
)
"%VENV_PY%" --version

REM uv venvs ship without pip by default -> install it if missing.
"%VENV_PY%" -m pip --version >nul 2>&1
if %errorlevel% equ 0 goto :install_deps
echo --^> pip missing in venv, installing pip...
"%VENV_PY%" -m ensurepip --upgrade >nul 2>&1
if %errorlevel% equ 0 (
    echo --^> pip installed via ensurepip
    goto :install_deps
)
if "%HAVE_UV_VENV%"=="1" (
    uv pip install --python "%VENV_PY%" pip
    if %errorlevel% neq 0 exit /b 1
    echo --^> pip installed via uv
    goto :install_deps
)
echo ERROR: cannot install pip into the venv. 1>&2
exit /b 1

:install_deps
echo --^> Installing session deps: Pytdbot[tdjson] pydantic-settings python-dotenv
"%VENV_PY%" -m pip install --upgrade pip
if %errorlevel% neq 0 exit /b 1
"%VENV_PY%" -m pip install "Pytdbot[tdjson]>=0.10.0" pydantic-settings python-dotenv
if %errorlevel% neq 0 exit /b 1

echo --^> Running create_session.py ^(workdir: project root^)
cd /d "%PROJECT_ROOT%"
"%VENV_PY%" create_session.py
set "STATUS=%errorlevel%"

if "%STATUS%"=="0" (
    echo --^> Login OK, removing temporary venv
    rmdir /s /q "%VENV_DIR%"
) else (
    echo --^> create_session.py failed ^(exit %STATUS%^). Venv kept at: 1>&2
    echo     %VENV_DIR% 1>&2
    echo     Re-run this script to start fresh. 1>&2
)

exit /b %STATUS%

:ensure_uv
where uv >nul 2>&1
if %errorlevel% equ 0 exit /b 0
echo --^> uv not found, installing uv...
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
if %errorlevel% neq 0 (
    echo ERROR: uv installation failed. 1>&2
    exit /b 1
)
REM Make uv visible in this session (default install locations).
set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
where uv >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: uv not found after install. Check %%USERPROFILE%%\.local\bin 1>&2
    exit /b 1
)
for /f "delims=" %%V in ('uv --version') do echo --^> uv installed: %%V
exit /b 0
