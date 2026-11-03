@echo off
REM One-shot installer for Mila (Windows).
REM
REM Usage:
REM   start\install.bat                 (install everything that is missing)
REM   start\install.bat --qr            (first login via QR link instead of code)
REM   start\install.bat --no-reranker   (never download the reranker)
REM   start\install.bat --skip-session  (do not touch the Telegram session)
REM   start\install.bat --no-encrypt    (do not encrypt the TDLib database)
REM   start\install.bat --encrypt       (encrypt the TDLib database, default)
REM   start\install.bat --yes           (non-interactive: accept defaults)
REM
REM The script is idempotent: each step is skipped when its result already exists.
REM   1. .venv + requirements.txt (skip when the venv is healthy and deps match)
REM   2. .env from .env.example (skip when present); generates the TDLib key and
REM      waits for the user to fill the required fields.
REM   3. TDLib session (create_session.py; an already authorized session exits
REM      immediately, so it effectively verifies and skips). On the first login
REM      it asks whether to encrypt the database.
REM   4. BGE reranker (asks Y/n; default yes, skipped when the model is present).
REM   5. Observatory frontend (npm ci + npm run build; skipped when dist exists
REM      or npm is unavailable).
REM
REM It never starts the bot. Run it afterwards with: .venv\Scripts\python.exe main.py
setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "PROJECT_ROOT=%%~fI"
set "VENV_DIR=%PROJECT_ROOT%\.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "STAMP=%VENV_DIR%\.mila-requirements.sha256"
set "ENV_FILE=%PROJECT_ROOT%\.env"
set "ENV_EXAMPLE=%PROJECT_ROOT%\.env.example"
set "REQ_FILE=%PROJECT_ROOT%\requirements.txt"
set "RERANKER_DIR=%PROJECT_ROOT%\models\rerankers\BGE-Reranker-v2-m3"

set "ASSUME_YES=0"
set "RERANKER_MODE=ask"
set "ENCRYPT_MODE=ask"
set "RUN_SESSION=1"
set "QR_ARG="

REM ---------------------------------------------------------------- arguments
:parse_args
if "%~1"=="" goto :args_done
if /i "%~1"=="--qr" (
    set "QR_ARG=--qr"
    shift
    goto :parse_args
)
if /i "%~1"=="--yes" (
    set "ASSUME_YES=1"
    shift
    goto :parse_args
)
if /i "%~1"=="-y" (
    set "ASSUME_YES=1"
    shift
    goto :parse_args
)
if /i "%~1"=="--reranker" (
    set "RERANKER_MODE=yes"
    shift
    goto :parse_args
)
if /i "%~1"=="--no-reranker" (
    set "RERANKER_MODE=no"
    shift
    goto :parse_args
)
if /i "%~1"=="--encrypt" (
    set "ENCRYPT_MODE=yes"
    shift
    goto :parse_args
)
if /i "%~1"=="--no-encrypt" (
    set "ENCRYPT_MODE=no"
    shift
    goto :parse_args
)
if /i "%~1"=="--skip-session" (
    set "RUN_SESSION=0"
    shift
    goto :parse_args
)
if /i "%~1"=="--help" goto :usage
if /i "%~1"=="-h" goto :usage
echo ERROR: unknown argument: %~1 ^(see --help^) 1>&2
exit /b 1

:usage
echo Usage: start\install.bat [--qr] [--no-reranker] [--reranker] [--skip-session] [--encrypt] [--no-encrypt] [--yes]
exit /b 0

:args_done
echo === Mila installer ===
echo Project: %PROJECT_ROOT%

REM Everything below relies on relative paths (.env, requirements.txt, data/...).
cd /d "%PROJECT_ROOT%"
if errorlevel 1 (
    echo ERROR: cannot cd to %PROJECT_ROOT% 1>&2
    exit /b 1
)

REM ------------------------------------------------- 1. venv + dependencies
echo.
echo --^> Step 1/5: Python environment
if exist "%VENV_DIR%\" if not exist "%VENV_PY%" (
    echo ERROR: %VENV_DIR% exists but %VENV_PY% is missing. 1>&2
    echo        Remove the broken venv and re-run. 1>&2
    exit /b 1
)

if exist "%VENV_PY%" (
    echo --^> Using existing venv: %VENV_DIR%
    goto :venv_ready
)

REM Try the py launcher with 3.11.
py -3.11 --version >nul 2>&1
if not errorlevel 1 (
    set "PYBIN=py -3.11"
    echo --^> Found system Python 3.11 via py launcher
    goto :make_venv
)

REM Try plain python and check for 3.11 in its version string.
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
    goto :venv_ready
)
echo --^> python -m venv failed, falling back to uv
if exist "%VENV_DIR%\" rmdir /s /q "%VENV_DIR%" 2>nul

:via_uv
call :ensure_uv
if errorlevel 1 exit /b 1
uv venv --python 3.11 "%VENV_DIR%"
if errorlevel 1 exit /b 1
echo --^> venv created with uv ^(Python 3.11^)

:venv_ready
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
if not errorlevel 1 goto :deps_check
echo --^> pip missing in venv, installing pip...
"%VENV_PY%" -m ensurepip --upgrade >nul 2>&1
if not errorlevel 1 (
    "%VENV_PY%" -m pip --version >nul 2>&1
    if not errorlevel 1 goto :deps_check
)
call :ensure_uv
if errorlevel 1 exit /b 1
uv pip install --python "%VENV_PY%" pip
if errorlevel 1 (
    echo ERROR: cannot install pip into the venv. 1>&2
    exit /b 1
)
echo --^> pip installed via uv

:deps_check
if not exist "%REQ_FILE%" (
    echo ERROR: requirements.txt not found at %REQ_FILE% 1>&2
    exit /b 1
)
"%VENV_PY%" -c "import hashlib,sys,pathlib;h=hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest();s=pathlib.Path(sys.argv[2]);sys.exit(0 if s.exists() and s.read_text().strip()==h else 1)" "%REQ_FILE%" "%STAMP%" >nul 2>&1
if errorlevel 1 goto :deps_install
"%VENV_PY%" -c "import pytdbot, faiss, fastapi, openai, sentence_transformers" >nul 2>&1
if errorlevel 1 goto :deps_install
echo --^> Dependencies already installed ^(requirements.txt unchanged^), skipping
goto :env_step

:deps_install
echo --^> Installing dependencies from requirements.txt ^(may take a while^)...
"%VENV_PY%" -m pip install --upgrade pip
if errorlevel 1 exit /b 1
"%VENV_PY%" -m pip install -r "%REQ_FILE%"
if errorlevel 1 (
    echo ERROR: dependency installation failed. 1>&2
    exit /b 1
)
"%VENV_PY%" -c "import hashlib,sys;open(sys.argv[2],'w').write(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "%REQ_FILE%" "%STAMP%"
echo --^> Dependencies installed

REM ------------------------------------------------------------------- 2. .env
:env_step
echo.
echo --^> Step 2/5: Configuration ^(.env^)
if exist "%ENV_FILE%" (
    echo --^> .env already exists, skipping creation
    goto :env_check
)

if not exist "%ENV_EXAMPLE%" (
    echo ERROR: .env.example not found at %ENV_EXAMPLE% 1>&2
    exit /b 1
)
copy /y "%ENV_EXAMPLE%" "%ENV_FILE%" >nul
if errorlevel 1 (
    echo ERROR: cannot create .env 1>&2
    exit /b 1
)
findstr /B /C:"TD_DATABASE_ENCRYPTION_KEY=" "%ENV_FILE%" >nul 2>&1
if not errorlevel 1 goto :env_created
"%VENV_PY%" "%PROJECT_ROOT%\start\set_env.py" TD_DATABASE_ENCRYPTION_KEY --random
if errorlevel 1 (
    echo ERROR: cannot write the TDLib key to .env 1>&2
    exit /b 1
)
:env_created
echo --^> .env created from .env.example with a generated TDLib key

:env_check
"%VENV_PY%" "%PROJECT_ROOT%\start\check_config.py" >nul 2>&1
if not errorlevel 1 goto :env_ready
echo.
"%VENV_PY%" "%PROJECT_ROOT%\start\check_config.py"
echo Edit %ENV_FILE% and fill in the values above ^(see comments inside the file^).
if "%ASSUME_YES%"=="1" (
    echo ERROR: cannot continue in non-interactive mode with an invalid .env 1>&2
    exit /b 1
)
set /p "=Press Enter after editing .env ^(Ctrl-C to abort^)... "
goto :env_check

:env_ready
echo --^> .env is ready

REM --------------------------------------------------------- 3. Telegram session
echo.
echo --^> Step 3/5: Telegram session
if "%RUN_SESSION%"=="0" (
    echo --^> --skip-session given, skipping
    goto :reranker_step
)

cd /d "%PROJECT_ROOT%"
if errorlevel 1 (
    echo ERROR: cannot cd to %PROJECT_ROOT% 1>&2
    exit /b 1
)
"%VENV_PY%" -c "from config import settings;print(settings.TD_FILES_DIRECTORY)" > "%VENV_DIR%\.tddir.tmp" 2>nul
set "TD_DIR="
set /p TD_DIR=<"%VENV_DIR%\.tddir.tmp"
del "%VENV_DIR%\.tddir.tmp" >nul 2>&1
set "TD_EXISTS="
if defined TD_DIR if exist "%TD_DIR%" set "TD_EXISTS=1"
if defined TD_DIR if exist "%PROJECT_ROOT%\%TD_DIR%" set "TD_EXISTS=1"
if defined TD_EXISTS goto :session_existing

echo --^> No TDLib data yet, starting the first login...
set "ENCRYPT=1"
if /i "%ENCRYPT_MODE%"=="no" set "ENCRYPT=0"
if /i "%ENCRYPT_MODE%"=="yes" goto :encrypt_decided
if /i "%ENCRYPT_MODE%"=="ask" if "%ASSUME_YES%"=="1" goto :encrypt_decided
if /i "%ENCRYPT_MODE%"=="ask" (
    set "ANS="
    set /p "ANS=Encrypt the TDLib database? ^(recommended^) [Y/n]: "
    if /i "!ANS!"=="n" set "ENCRYPT=0"
    if /i "!ANS!"=="no" set "ENCRYPT=0"
)

:encrypt_decided
if "%ENCRYPT%"=="1" goto :session_encrypt
"%VENV_PY%" "%PROJECT_ROOT%\start\set_env.py" TD_DATABASE_ENCRYPTION_KEY --empty
if errorlevel 1 (
    echo ERROR: cannot clear TD_DATABASE_ENCRYPTION_KEY in .env 1>&2
    exit /b 1
)
echo WARNING: TDLib database will NOT be encrypted ^(key cleared in .env^). 1>&2
goto :session_run

:session_encrypt
findstr /R /C:"^TD_DATABASE_ENCRYPTION_KEY=." "%ENV_FILE%" >nul 2>&1
if not errorlevel 1 goto :session_encrypt_ok
"%VENV_PY%" "%PROJECT_ROOT%\start\set_env.py" TD_DATABASE_ENCRYPTION_KEY --random
if errorlevel 1 (
    echo ERROR: cannot generate TD_DATABASE_ENCRYPTION_KEY. 1>&2
    exit /b 1
)
:session_encrypt_ok
echo --^> TDLib database will be encrypted
goto :session_run

:session_existing
echo --^> Existing TDLib data found, verifying the session...
if not "%ENCRYPT_MODE%"=="ask" echo WARNING: encryption flag ignored: the existing database keeps its current mode. 1>&2

:session_run
"%VENV_PY%" create_session.py %QR_ARG%
if errorlevel 1 (
    echo ERROR: Telegram login failed. Fix the error above and re-run this script. 1>&2
    exit /b 1
)
echo --^> Telegram session is ready

REM ---------------------------------------------------------------- 4. reranker
:reranker_step
echo.
echo --^> Step 4/5: Reranker model
if exist "%RERANKER_DIR%\" (
    echo --^> Reranker already present at %RERANKER_DIR%, skipping
    goto :frontend_step
)
if /i "%RERANKER_MODE%"=="no" (
    echo --^> --no-reranker given, skipping
    goto :frontend_step
)
if /i "%RERANKER_MODE%"=="yes" goto :reranker_install
if "%ASSUME_YES%"=="1" goto :reranker_install
set "ANS="
set /p "ANS=Download BGE-Reranker-v2-m3 ^(~2 GB^)? [Y/n]: "
if /i "!ANS!"=="n" goto :reranker_skip
if /i "!ANS!"=="no" goto :reranker_skip
goto :reranker_install

:reranker_skip
echo --^> Skipping reranker
goto :frontend_step

:reranker_install
echo --^> Downloading reranker ^(this can take a while^)...
cd /d "%PROJECT_ROOT%"
"%VENV_PY%" install_reranker.py
if errorlevel 1 (
    echo WARNING: reranker download failed; search_memory will use FAISS order ^(lower quality^). 1>&2
    echo          Retry later with: "%VENV_PY%" install_reranker.py 1>&2
) else (
    echo --^> Reranker installed
)

REM ---------------------------------------------------------------- 5. frontend
:frontend_step
echo.
echo --^> Step 5/5: Observatory frontend
call :build_frontend
goto :done

:done
echo.
echo === Installation complete ===
echo Bot:      cd /d "%PROJECT_ROOT%" ^&^& .venv\Scripts\python.exe main.py
echo Panel:    cd /d "%PROJECT_ROOT%" ^&^& .venv\Scripts\python.exe -m Mila_observatory.server
echo Frontend: cd /d "%PROJECT_ROOT%\Mila_observatory\frontend" ^&^& npm ci ^&^& npm run build
echo           ^(для разработки: npm run dev, Vite на :5173^)
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
set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
where uv >nul 2>&1
if errorlevel 1 (
    echo ERROR: uv not found after install. Check %USERPROFILE%\.local\bin 1>&2
    exit /b 1
)
for /f "delims=" %%V in ('uv --version') do echo --^> uv installed: %%V
exit /b 0

REM ---------------------------------------------------------------------------
REM Build the Observatory frontend when Node.js is available.
REM ---------------------------------------------------------------------------
:build_frontend
if exist "%PROJECT_ROOT%\Mila_observatory\frontend\dist\index.html" (
    echo --^> Frontend already built, skipping
    exit /b 0
)
where npm >nul 2>&1
if errorlevel 1 (
    echo WARNING: npm not found; the panel will start without UI. 1>&2
    echo          Install Node.js 20+ and run: cd /d "%PROJECT_ROOT%\Mila_observatory\frontend" ^&^& npm ci ^&^& npm run build 1>&2
    echo          Or use Docker: docker compose up -d --build 1>&2
    exit /b 0
)
echo --^> Building Observatory frontend ^(npm ci ^&^& npm run build^)...
pushd "%PROJECT_ROOT%\Mila_observatory\frontend"
call npm ci
if errorlevel 1 goto :frontend_build_failed
call npm run build
if errorlevel 1 goto :frontend_build_failed
popd
echo --^> Frontend built
exit /b 0

:frontend_build_failed
popd
echo WARNING: frontend build failed; the panel will start without UI. 1>&2
echo          Retry: cd /d "%PROJECT_ROOT%\Mila_observatory\frontend" ^&^& npm ci ^&^& npm run build 1>&2
exit /b 0
